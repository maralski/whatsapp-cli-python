// Scoped history requests on an existing linked device. No pairing or human sends.
package main

import (
	"compress/zlib"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

var sourceDigest = "unbuilt"
var phoneRE = regexp.MustCompile(`^[1-9][0-9]{6,14}@s\.whatsapp\.net$`)
var lidRE = regexp.MustCompile(`^[1-9][0-9]{5,19}(?:@lid)?$`)
var idRE = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

type boundedTransport struct{}

func (boundedTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	resp, err := http.DefaultTransport.RoundTrip(req)
	if err != nil {
		return nil, err
	}
	if resp.ContentLength > 16*1024*1024 {
		resp.Body.Close()
		return nil, errors.New("download budget")
	}
	resp.Body = struct {
		io.Reader
		io.Closer
	}{io.LimitReader(resp.Body, 16*1024*1024+1), resp.Body}
	return resp, nil
}

type row struct {
	ID        string  `json:"id"`
	TS        int64   `json:"ts"`
	FromMe    bool    `json:"from_me"`
	Text      *string `json:"text"`
	Revoked   string  `json:"revoke_id,omitempty"`
	Truncated bool    `json:"text_truncated"`
}
type request struct {
	Mode     string `json:"mode,omitempty"`
	Store    string `json:"store"`
	Identity string `json:"identity"`
	Chat     string `json:"chat"`
	Anchor   row    `json:"anchor"`
	Count    int    `json:"count"`
	Seconds  int    `json:"seconds"`
}
type response struct {
	Status           string         `json:"status,omitempty"`
	Error            string         `json:"error,omitempty"`
	Rows             []row          `json:"rows,omitempty"`
	PhoneEnd         bool           `json:"phone_reported_end"`
	MoreInaccessible bool           `json:"phone_reports_inaccessible"`
	Truncated        bool           `json:"response_truncated"`
	Refresh          *refreshReport `json:"refresh,omitempty"`
}

func private(path string, directory bool) bool {
	info, err := os.Lstat(path)
	if err != nil || info.Mode().Perm()&0077 != 0 {
		return false
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok || st.Uid != uint32(os.Getuid()) {
		return false
	}
	if directory {
		return info.IsDir()
	}
	return info.Mode().IsRegular() && st.Nlink == 1
}
func valid(r request) bool {
	base := filepath.IsAbs(r.Store) && filepath.Clean(r.Store) == r.Store &&
		!strings.ContainsAny(r.Store, "?#\x00") && phoneRE.MatchString(r.Chat) &&
		r.Seconds >= 1 && r.Seconds <= 120
	if r.Mode == "refresh" {
		return base && r.Count >= 1 && r.Count <= 200 && r.Anchor.ID == "" && r.Anchor.TS == 0
	}
	return base && r.Mode == "" && idRE.MatchString(r.Anchor.ID) && r.Anchor.TS > 0 &&
		r.Anchor.TS <= time.Now().Unix()+300 && r.Count >= 1 && r.Count <= 50
}
func verifiedAliases(ctx context.Context, db *sql.DB, chat string) map[string]bool {
	aliases := map[string]bool{chat: true}
	var kind, definition string
	if db.QueryRowContext(ctx, "SELECT type,sql FROM sqlite_master WHERE name='whatsmeow_lid_map'").Scan(&kind, &definition) != nil || kind != "table" || !strings.HasPrefix(strings.ToUpper(definition), "CREATE TABLE") {
		return aliases
	}
	pn := strings.Split(chat, "@")[0]
	rs, err := db.QueryContext(ctx, "SELECT lid,pn FROM whatsmeow_lid_map WHERE pn IN (?,?) LIMIT 2", pn, chat)
	if err != nil {
		return aliases
	}
	var lid, mapped string
	count := 0
	for rs.Next() {
		count++
		if rs.Scan(&lid, &mapped) != nil {
			count = 2
			break
		}
	}
	rs.Close()
	if count != 1 || !lidRE.MatchString(lid) || (mapped != pn && mapped != chat) {
		return aliases
	}
	user := strings.Split(lid, "@")[0]
	rs, err = db.QueryContext(ctx, "SELECT lid,pn FROM whatsmeow_lid_map WHERE lid IN (?,?) LIMIT 2", user, user+"@lid")
	if err != nil {
		return aliases
	}
	count = 0
	for rs.Next() {
		var reverseLID, reversePN string
		count++
		if rs.Scan(&reverseLID, &reversePN) != nil || reverseLID != lid || reversePN != mapped {
			count = 2
			break
		}
	}
	rs.Close()
	if count == 1 {
		aliases[user+"@lid"] = true
	}
	return aliases
}

// Retain anchors for non-text messages; wrappers and expiring text are never unpacked.
func messageRow(id string, ts int64, outgoing bool, msg *waE2E.Message, aliases map[string]bool) (row, bool) {
	r := row{ID: id, TS: ts, FromMe: outgoing}
	if !idRE.MatchString(id) || ts <= 0 || ts > time.Now().Unix()+300 || msg == nil {
		return r, false
	}
	if p := msg.GetProtocolMessage(); p != nil {
		key := p.GetKey()
		if p.GetType() == waE2E.ProtocolMessage_REVOKE && idRE.MatchString(key.GetID()) && aliases[key.GetRemoteJID()] {
			r.Revoked = key.GetID()
		}
		return r, true
	}
	fields := 0
	msg.ProtoReflect().Range(func(field protoreflect.FieldDescriptor, _ protoreflect.Value) bool {
		if field.Name() != "messageContextInfo" {
			fields++
		}
		return true
	})
	var text string
	if fields == 1 && msg.Conversation != nil {
		text = msg.GetConversation()
	}
	if fields == 1 && msg.ExtendedTextMessage != nil {
		context := msg.GetExtendedTextMessage().GetContextInfo()
		if context.GetExpiration() == 0 && context.GetEphemeralSettingTimestamp() == 0 {
			text = msg.GetExtendedTextMessage().GetText()
		}
	}
	if strings.TrimSpace(text) != "" && !strings.ContainsRune(text, 0) {
		runes := []rune(text)
		if len(runes) > 10000 {
			text = string(runes[:10000])
			r.Truncated = true
		}
		r.Text = &text
	}
	return r, true
}
func selected(data *waHistorySync.HistorySync, aliases map[string]bool, anchor row, count int) (response, bool) {
	out := response{Status: "received", Rows: []row{}}
	matched := false
	if data == nil || data.GetSyncType() != waHistorySync.HistorySync_ON_DEMAND {
		return out, false
	}
	for _, conv := range data.GetConversations() {
		if !aliases[conv.GetID()] {
			continue
		}
		matched = true
		out.PhoneEnd = out.PhoneEnd || conv.GetEndOfHistoryTransferType() == waHistorySync.Conversation_COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY
		out.MoreInaccessible = out.MoreInaccessible || conv.GetEndOfHistoryTransferType() == waHistorySync.Conversation_COMPLETE_ON_DEMAND_SYNC_WITH_MORE_MSG_ON_PRIMARY_BUT_NO_ACCESS
		if conv.GetEphemeralExpiration() != 0 {
			continue
		}
		for _, item := range conv.GetMessages() {
			msg := item.GetMessage()
			key := msg.GetKey()
			if !aliases[key.GetRemoteJID()] {
				continue
			}
			r, ok := messageRow(key.GetID(), int64(msg.GetMessageTimestamp()), key.GetFromMe(), msg.GetMessage(), aliases)
			// Equal timestamp IDs are valid: phone ordering need not match ID ordering.
			if !ok || r.TS > anchor.TS || r.ID == anchor.ID {
				continue
			}
			if len(out.Rows) >= count {
				out.Truncated = true
				continue
			}
			out.Rows = append(out.Rows, r)
		}
	}
	return out, matched
}

// Whatsmeow's standard decoder has an unbounded decompression allocation.
// This read-only helper decodes a bounded blob and does not mirror unrelated
// conversation metadata or secrets into an application archive.
type historyDownloader interface {
	Download(context.Context, whatsmeow.DownloadableMessage) ([]byte, error)
}

func download(ctx context.Context, client historyDownloader, notif *waE2E.HistorySyncNotification) (*waHistorySync.HistorySync, error) {
	data := notif.GetInitialHistBootstrapInlinePayload()
	if len(data) == 0 {
		var err error
		data, err = client.Download(ctx, notif)
		if err != nil {
			return nil, err
		}
	}
	if len(data) > 16*1024*1024 {
		return nil, errors.New("blob budget")
	}
	reader, err := zlib.NewReader(strings.NewReader(string(data)))
	if err != nil {
		return nil, err
	}
	defer reader.Close()
	raw, err := io.ReadAll(io.LimitReader(reader, 16*1024*1024+1))
	if err != nil || len(raw) > 16*1024*1024 {
		return nil, errors.New("decode budget")
	}
	result := new(waHistorySync.HistorySync)
	err = proto.UnmarshalOptions{DiscardUnknown: true}.Unmarshal(raw, result)
	return result, err
}

func operate(r request) response {
	if !valid(r) || !private(r.Store, true) {
		return response{Error: "invalid_scope"}
	}
	for parent := filepath.Dir(r.Store); ; parent = filepath.Dir(parent) {
		info, err := os.Lstat(parent)
		if err != nil || !info.IsDir() {
			return response{Error: "unsafe_store"}
		}
		st, ok := info.Sys().(*syscall.Stat_t)
		if !ok || (st.Uid != 0 && st.Uid != uint32(os.Getuid())) || (info.Mode().Perm()&0022 != 0 && !(st.Uid == 0 && info.Mode()&os.ModeSticky != 0)) {
			return response{Error: "unsafe_store"}
		}
		if parent == filepath.Dir(parent) {
			break
		}
	}
	// Python validates every ancestor and account marker before launch; recheck
	// the concrete store and every native sidecar immediately before opening.
	for _, name := range []string{"account.json", "session.db", "messages.sqlite3", ".cli.lock"} {
		if !private(filepath.Join(r.Store, name), false) {
			return response{Error: "unsafe_store"}
		}
	}
	for _, suffix := range []string{"-wal", "-shm", "-journal"} {
		path := filepath.Join(r.Store, "session.db"+suffix)
		if _, err := os.Lstat(path); err == nil && !private(path, false) {
			return response{Error: "unsafe_store"}
		}
	}
	fd, err := syscall.Open(filepath.Join(r.Store, ".cli.lock"), syscall.O_RDWR|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0600)
	if err != nil {
		return response{Error: "store_access_denied"}
	}
	defer syscall.Close(fd)
	if syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		return response{Error: "backend_busy"}
	}
	defer syscall.Flock(fd, syscall.LOCK_UN)
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(r.Seconds+25)*time.Second)
	defer cancel()
	uri := (&url.URL{Scheme: "file", Path: filepath.Join(r.Store, "session.db")}).String() + "?mode=rw&_foreign_keys=on&_busy_timeout=1000"
	container, err := sqlstore.New(ctx, "sqlite3", uri, waLog.Noop)
	if err != nil {
		return response{Error: "store_unavailable"}
	}
	defer container.Close()
	devices, err := container.GetAllDevices(ctx)
	if err != nil || len(devices) != 1 || devices[0].ID == nil || devices[0].ID.String() != r.Identity {
		return response{Error: "account_changed"}
	}
	metadata, err := sql.Open("sqlite3", uri)
	if err != nil {
		return response{Error: "store_unavailable"}
	}
	metadata.ExecContext(ctx, "PRAGMA trusted_schema=OFF")
	aliases := verifiedAliases(ctx, metadata, r.Chat)
	metadata.Close()
	client := whatsmeow.NewClient(devices[0], waLog.Noop)
	client.SetMediaHTTPClient(&http.Client{Transport: boundedTransport{}, Timeout: time.Duration(r.Seconds+20) * time.Second})
	client.ManualHistorySyncDownload = true
	client.EnableAutoReconnect = false
	client.InitialAutoReconnect = false
	client.AutomaticMessageRerequestFromPhone = false
	if r.Mode == "refresh" {
		return refresh(ctx, client, r, aliases)
	}
	responses := make(chan response, 1)
	connected := make(chan struct{}, 1)
	stopped := make(chan struct{}, 1)
	client.AddEventHandler(func(event any) {
		switch e := event.(type) {
		case *events.Connected:
			select {
			case connected <- struct{}{}:
			default:
			}
		case *events.LoggedOut, *events.PairSuccess, *events.Disconnected:
			select {
			case stopped <- struct{}{}:
			default:
			}
		case *events.Message:
			notif := e.Message.GetProtocolMessage().GetHistorySyncNotification()
			if notif == nil || notif.GetSyncType() != waE2E.HistorySyncType_ON_DEMAND {
				return
			}
			data, err := download(ctx, client, notif)
			if err != nil {
				select {
				case responses <- response{Error: "history_download_failed"}:
				default:
				}
				return
			}
			if result, ok := selected(data, aliases, r.Anchor, r.Count); ok {
				select {
				case responses <- result:
				default:
				}
			}
		}
	})
	defer client.Disconnect()
	if client.ConnectContext(ctx) != nil {
		return response{Error: "not_connected"}
	}
	select {
	case <-connected:
	case <-stopped:
		return response{Error: "account_stopped"}
	case <-time.After(20 * time.Second):
		return response{Error: "connect_timeout"}
	case <-ctx.Done():
		return response{Error: "connect_timeout"}
	}
	address := r.Chat
	for alias := range aliases {
		if strings.HasSuffix(alias, "@lid") {
			address = alias
		}
	}
	chat, _ := types.ParseJID(address)
	info := &types.MessageInfo{MessageSource: types.MessageSource{Chat: chat, IsFromMe: r.Anchor.FromMe}, ID: r.Anchor.ID, Timestamp: time.Unix(r.Anchor.TS, 0)}
	// SendPeerMessage addresses the user's primary device. No contact message,
	// pairing, mark-read, media download, or automatic request retry exists here.
	if _, err = client.SendPeerMessage(ctx, client.BuildHistorySyncRequest(info, r.Count)); err != nil {
		return response{Error: "history_request_failed"}
	}
	select {
	case result := <-responses:
		return result
	case <-stopped:
		return response{Error: "account_stopped"}
	case <-time.After(time.Duration(r.Seconds) * time.Second):
		return response{Status: "response_timeout"}
	case <-ctx.Done():
		return response{Status: "response_timeout"}
	}
}
func main() {
	syscall.Umask(0077)
	log.SetOutput(io.Discard)
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		json.NewEncoder(os.Stdout).Encode(map[string]any{"abi": 1, "source_sha256": sourceDigest, "go": runtime.Version()})
		return
	}
	result := response{Error: "invalid_request"}
	func() {
		defer func() {
			if recover() != nil {
				result = response{Error: "history_failed"}
			}
		}()
		raw, err := io.ReadAll(io.LimitReader(os.Stdin, 65537))
		if err != nil || len(raw) > 65536 {
			return
		}
		var req request
		decoder := json.NewDecoder(strings.NewReader(string(raw)))
		decoder.DisallowUnknownFields()
		if decoder.Decode(&req) != nil || decoder.Decode(new(any)) != io.EOF {
			return
		}
		result = operate(req)
	}()
	json.NewEncoder(os.Stdout).Encode(result)
}
