package main

import (
	"bytes"
	"compress/zlib"
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

func liveFixture(address, id, text string, ts int64) *events.Message {
	jid, _ := types.ParseJID(address)
	return &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: jid, Sender: jid}, ID: id, Timestamp: time.Unix(ts, 0)}, Message: &waE2E.Message{Conversation: proto.String(text)}}
}

func TestRefreshScopeNewestHeadersAndFlags(t *testing.T) {
	s := newRefreshState(2)
	aliases := map[string]bool{chat: true, "12345678901234@lid": true}
	s.live(liveFixture(other, "FOREIGN", "unrelated", 100), aliases)
	s.live(liveFixture(chat, "OLD", "older", 100), aliases)
	s.live(liveFixture(chat, "NEW", "newest", 300), aliases)
	private := liveFixture("12345678901234@lid", "PRIVATE", "hidden", 200)
	private.IsViewOnceV2 = true
	s.live(private, aliases)
	r := s.result()
	if len(r.Rows) != 2 || r.Rows[0].ID != "NEW" || r.Rows[1].ID != "PRIVATE" || r.Rows[1].Text != nil || !r.Truncated || r.Refresh.Live != 3 {
		t.Fatal("scope, newest retention or view-once privacy")
	}
}

func TestRefreshRecentHistoryDoesNotUseOldestAnchor(t *testing.T) {
	s := newRefreshState(20)
	data := &waHistorySync.HistorySync{SyncType: waHistorySync.HistorySync_RECENT.Enum(), Conversations: []*waHistorySync.Conversation{
		{ID: proto.String(other), Messages: []*waHistorySync.HistorySyncMsg{historyItem(other, "OTHER", "unrelated", 100)}},
		{ID: proto.String(chat), Messages: []*waHistorySync.HistorySyncMsg{historyItem(other, "BAD_KEY", "unrelated", 100), historyItem(chat, "NEWER", "recent", 1700000000)}},
	}}
	s.history(data, map[string]bool{chat: true})
	if r := s.result(); len(r.Rows) != 1 || r.Rows[0].ID != "NEWER" || r.Rows[0].TS != 1700000000 {
		t.Fatal("recent history scoped or timestamp mismatch")
	}
	data.SyncType = waHistorySync.HistorySync_ON_DEMAND.Enum()
	s.history(data, map[string]bool{chat: true})
	if s.result().Refresh.History != 1 {
		t.Fatal("mixed in unrelated backfill response")
	}
}

func TestRecoveryUsesRealSelectedIDsOnceWithBudget(t *testing.T) {
	s := newRefreshState(200)
	aliases := map[string]bool{chat: true}
	base := &events.UndecryptableMessage{Info: liveFixture(chat, "REAL_ID", "", 100).Info}
	if !s.recovery(base, aliases) || s.recovery(base, aliases) {
		t.Fatal("duplicate recovery")
	}
	for _, e := range []*events.UndecryptableMessage{
		{Info: liveFixture(other, "FOREIGN", "", 100).Info},
		{Info: liveFixture(chat, "VIEW", "", 100).Info, UnavailableType: events.UnavailableTypeViewOnce},
		{Info: liveFixture(chat, "HIDE", "", 100).Info, DecryptFailMode: events.DecryptFailHide},
		{Info: liveFixture(chat, "UNAVAILABLE", "", 100).Info, IsUnavailable: true},
		{Info: liveFixture(chat, "bad/id", "", 100).Info},
	} {
		if s.recovery(e, aliases) {
			t.Fatal("unsafe recovery")
		}
	}
	for i := 0; i < 30; i++ {
		e := *base
		e.Info.ID = "ID_" + strings.Repeat("A", i+1)
		s.recovery(&e, aliases)
	}
	if len(s.attempted) != 20 {
		t.Fatal("unbounded recovery IDs")
	}
}

func TestRefreshBodyBudgetAndNoInventedAnchor(t *testing.T) {
	s := newRefreshState(200)
	for i := 0; i < 200; i++ {
		text := strings.Repeat("\t", 10000)
		s.add(row{ID: "ID_" + strings.Repeat("A", i%100+1) + strings.Repeat("B", i/100), TS: int64(i + 1), Text: &text})
	}
	n := 0
	for _, r := range s.result().Rows {
		if r.Text != nil {
			n += len(*r.Text)
		}
	}
	if n > 512*1024 || !s.result().Truncated {
		t.Fatal("response body budget")
	}
	r := request{Mode: "refresh", Store: "/private/account", Chat: chat, Count: 200, Seconds: 30}
	if !valid(r) {
		t.Fatal("refresh incorrectly requires an anchor")
	}
	r.Anchor = row{ID: "FAKE", TS: 100}
	if valid(r) {
		t.Fatal("refresh accepts injected anchor")
	}
}

type fakeRefreshClient struct {
	mu            sync.Mutex
	handler       whatsmeow.EventHandler
	events        []any
	online        bool
	states        []types.Presence
	peers         []*waE2E.Message
	presenceError bool
	connectError  bool
}

func (f *fakeRefreshClient) AddEventHandler(h whatsmeow.EventHandler) uint32 { f.handler = h; return 1 }
func (f *fakeRefreshClient) RemoveEventHandler(_ uint32) bool                { return true }
func (f *fakeRefreshClient) ConnectContext(_ context.Context) error {
	f.online = true
	f.handler(&events.Connected{})
	for _, e := range f.events {
		f.handler(e)
	}
	if f.connectError {
		return errors.New("synthetic-private-endpoint")
	}
	return nil
}
func (f *fakeRefreshClient) Disconnect()       { f.online = false }
func (f *fakeRefreshClient) IsConnected() bool { return f.online }
func (f *fakeRefreshClient) SendPresence(_ context.Context, value types.Presence) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.states = append(f.states, value)
	if f.presenceError {
		return errors.New("synthetic-private-endpoint")
	}
	return nil
}
func (f *fakeRefreshClient) SendPeerMessage(_ context.Context, m *waE2E.Message) (whatsmeow.SendResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.peers = append(f.peers, m)
	return whatsmeow.SendResponse{}, nil
}
func (f *fakeRefreshClient) BuildUnavailableMessageRequest(chat, sender types.JID, id string) *waE2E.Message {
	return new(whatsmeow.Client).BuildUnavailableMessageRequest(chat, sender, id)
}
func (f *fakeRefreshClient) Download(_ context.Context, _ whatsmeow.DownloadableMessage) ([]byte, error) {
	return nil, errors.New("no network in tests")
}

func TestRefreshLifecycleOfflineReplayAndPresenceCleanup(t *testing.T) {
	f := &fakeRefreshClient{events: []any{&events.OfflineSyncPreview{}, liveFixture(chat, "RECENT", "retrieved", 1700000000), &events.OfflineSyncCompleted{}, &events.PushNameSetting{}}}
	r := refresh(context.Background(), f, request{Count: 20, Seconds: 1}, map[string]bool{chat: true})
	if r.Status != "refreshed" || len(r.Rows) != 1 || !r.Refresh.OfflineComplete || !r.Refresh.OfflinePreview || !r.Refresh.PresenceAvailable || !r.Refresh.PresenceUnavailable {
		t.Fatal("replay diagnostics")
	}
	if f.states[len(f.states)-1] != types.PresenceUnavailable || len(f.peers) != 0 || f.online {
		t.Fatal("presence cleanup or human send")
	}
	f.handler(&events.Connected{})
	if f.states[len(f.states)-1] != types.PresenceUnavailable {
		t.Fatal("late callback made device online")
	}
}

func TestRefreshCleanupOnConnectErrorAndPresenceFailure(t *testing.T) {
	for _, failConnect := range []bool{false, true} {
		f := &fakeRefreshClient{connectError: failConnect, presenceError: true}
		r := refresh(context.Background(), f, request{Count: 20, Seconds: 1}, map[string]bool{chat: true})
		if f.states[len(f.states)-1] != types.PresenceUnavailable || f.online {
			t.Fatal("cleanup omitted")
		}
		if failConnect && r.Error != "not_connected" {
			t.Fatal("raw exception exposed")
		}
		if !failConnect && (r.Refresh.PresenceAvailable || r.Refresh.PresenceUnavailable || r.Refresh.PresenceFailures != 2) {
			t.Fatal("false presence success")
		}
	}
}

func TestRefreshRecoveryWaitsAndCancelsAfterReadableCopy(t *testing.T) {
	info := liveFixture(chat, "REAL_ID", "", 1700000000).Info
	for _, recovered := range []bool{false, true} {
		f := &fakeRefreshClient{events: []any{&events.UndecryptableMessage{Info: info}}}
		if recovered {
			f.events = append(f.events, liveFixture(chat, "REAL_ID", "readable", 1700000000))
			f.events = append(f.events, liveFixture(chat, "NEWER_ID", "evicts older row", 1700000001))
		}
		r := refresh(context.Background(), f, request{Count: 1, Seconds: 6}, map[string]bool{chat: true})
		want := 1
		if recovered {
			want = 0
		}
		if len(f.peers) != want || r.Refresh.RecoveryRequests != want {
			t.Fatal("recovery duplicate or missing")
		}
		if want == 1 {
			key := f.peers[0].GetProtocolMessage().GetPeerDataOperationRequestMessage().GetPlaceholderMessageResendRequest()[0].GetMessageKey()
			if key.GetID() != "REAL_ID" || key.GetRemoteJID() != chat || f.peers[0].GetConversation() != "" {
				t.Fatal("wrong recovery target or human body")
			}
		}
	}
}

func TestRefreshDecodesAvailableHistoryNotification(t *testing.T) {
	data := &waHistorySync.HistorySync{SyncType: waHistorySync.HistorySync_RECENT.Enum(), Conversations: []*waHistorySync.Conversation{{ID: proto.String(chat), Messages: []*waHistorySync.HistorySyncMsg{historyItem(chat, "NEW", "newer", 1700000000)}}}}
	raw, _ := proto.Marshal(data)
	var compressed bytes.Buffer
	w := zlib.NewWriter(&compressed)
	w.Write(raw)
	w.Close()
	notif := &waE2E.HistorySyncNotification{SyncType: waE2E.HistorySyncType_RECENT.Enum(), InitialHistBootstrapInlinePayload: compressed.Bytes()}
	f := &fakeRefreshClient{events: []any{&events.Message{Message: &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{HistorySyncNotification: notif}}}}}
	r := refresh(context.Background(), f, request{Count: 20, Seconds: 1}, map[string]bool{chat: true})
	if len(r.Rows) != 1 || r.Rows[0].ID != "NEW" || r.Refresh.HistoryDownloads != 1 || r.Refresh.HistoryFailures != 0 {
		t.Fatal("available history skipped")
	}
}
