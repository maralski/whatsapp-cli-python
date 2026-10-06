package main

import (
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
	"testing"
	"time"
)

const chat = "12025550123@s.whatsapp.net"
const other = "12025550124@s.whatsapp.net"

func TestPeerProtocolRequestSeconds(t *testing.T) {
	client := new(whatsmeow.Client)
	jid, _ := types.ParseJID(chat)
	msg := client.BuildHistorySyncRequest(&types.MessageInfo{MessageSource: types.MessageSource{Chat: jid, IsFromMe: true}, ID: "KNOWN_ID", Timestamp: time.Unix(1700000000, 0)}, 50)
	p := msg.GetProtocolMessage()
	if p.GetType() != waE2E.ProtocolMessage_PEER_DATA_OPERATION_REQUEST_MESSAGE {
		t.Fatal("wrong protocol")
	}
	req := p.GetPeerDataOperationRequestMessage().GetHistorySyncOnDemandRequest()
	if req.GetChatJID() != chat || req.GetOldestMsgID() != "KNOWN_ID" || req.GetOldestMsgTimestampMS() != 1700000000 || req.GetOnDemandMsgCount() != 50 {
		t.Fatal("wrong anchor or timestamp units")
	}
	if msg.GetConversation() != "" {
		t.Fatal("human message")
	}
}
func historyItem(remote, id, text string, ts uint64) *waHistorySync.HistorySyncMsg {
	return &waHistorySync.HistorySyncMsg{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{RemoteJID: proto.String(remote), ID: proto.String(id)}, MessageTimestamp: proto.Uint64(ts), Message: &waE2E.Message{Conversation: proto.String(text)}}}
}
func TestScopeAndPrivacy(t *testing.T) {
	aliases := map[string]bool{chat: true, "12345678901234@lid": true}
	data := &waHistorySync.HistorySync{SyncType: waHistorySync.HistorySync_ON_DEMAND.Enum(), Conversations: []*waHistorySync.Conversation{
		{ID: proto.String(other), Messages: []*waHistorySync.HistorySyncMsg{historyItem(other, "OTHER", "unrelated", 100)}},
		{ID: proto.String(chat), Messages: []*waHistorySync.HistorySyncMsg{historyItem(other, "WRONG_KEY", "unrelated", 100), historyItem(chat, "NEWER", "newer", 201), historyItem("12345678901234@lid", "VALID", "selected", 100)}, EndOfHistoryTransferType: waHistorySync.Conversation_COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY.Enum()},
	}}
	result, ok := selected(data, aliases, row{ID: "ANCHOR", TS: 200}, 50)
	if !ok || len(result.Rows) != 1 || result.Rows[0].ID != "VALID" || !result.PhoneEnd {
		t.Fatal("chat/key scope or end marker")
	}
}
func TestNoWrapperOrExpiringBodies(t *testing.T) {
	aliases := map[string]bool{chat: true}
	messages := []*waE2E.Message{
		{EphemeralMessage: &waE2E.FutureProofMessage{Message: &waE2E.Message{Conversation: proto.String("private")}}},
		{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("private"), ContextInfo: &waE2E.ContextInfo{Expiration: proto.Uint32(60)}}},
		{ImageMessage: &waE2E.ImageMessage{Caption: proto.String("private")}},
	}
	for _, message := range messages {
		r, ok := messageRow("MEDIA_ANCHOR", 100, false, message, aliases)
		if !ok || r.Text != nil {
			t.Fatal("wrapper/media text exposed or header lost")
		}
	}
}
func TestRevocationExactChat(t *testing.T) {
	message := &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_REVOKE.Enum(), Key: &waCommon.MessageKey{RemoteJID: proto.String(other), ID: proto.String("DELETED")}}}
	r, _ := messageRow("REVOKE", 100, false, message, map[string]bool{chat: true})
	if r.Revoked != "" {
		t.Fatal("foreign revoke")
	}
	message.ProtocolMessage.Key.RemoteJID = proto.String(chat)
	r, _ = messageRow("REVOKE", 100, false, message, map[string]bool{chat: true})
	if r.Revoked != "DELETED" || r.Text != nil {
		t.Fatal("revoke missing")
	}
}
func TestBounds(t *testing.T) {
	r := request{Store: "/private/account", Chat: chat, Anchor: row{ID: "KNOWN", TS: 100}, Count: 50, Seconds: 60}
	if !valid(r) {
		t.Fatal("valid rejected")
	}
	for _, count := range []int{0, 51} {
		r.Count = count
		if valid(r) {
			t.Fatal("count bound")
		}
	}
}

func TestProtocolMetadataAlongsidePlainText(t *testing.T) {
	message := &waE2E.Message{Conversation: proto.String("selected text"), MessageContextInfo: &waE2E.MessageContextInfo{MessageSecret: []byte("synthetic-unused-metadata")}}
	r, ok := messageRow("META", 100, false, message, map[string]bool{chat: true})
	if !ok || r.Text == nil || *r.Text != "selected text" {
		t.Fatal("normal metadata blocked text")
	}
	message.ImageMessage = &waE2E.ImageMessage{Caption: proto.String("do not expose")}
	r, _ = messageRow("MIXED", 100, false, message, map[string]bool{chat: true})
	if r.Text != nil {
		t.Fatal("mixed media exposed")
	}
}
