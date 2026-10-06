// Bounded selected-chat adaptation of wacli's offline replay/presence recovery.
package main

import (
	"context"
	"sort"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

type refreshReport struct {
	Live                int  `json:"selected_live_events"`
	History             int  `json:"selected_history_events"`
	Undecryptable       int  `json:"selected_undecryptable_events"`
	RecoveryRequests    int  `json:"recovery_requests"`
	RecoveryFailures    int  `json:"recovery_failures"`
	HistoryDownloads    int  `json:"history_downloads"`
	HistoryFailures     int  `json:"history_download_failures"`
	OfflinePreview      bool `json:"offline_replay_announced"`
	OfflineComplete     bool `json:"offline_replay_completed"`
	PresenceAvailable   bool `json:"presence_available_sent"`
	PresenceUnavailable bool `json:"presence_unavailable_sent"`
	PresenceFailures    int  `json:"presence_failures"`
}

type refreshState struct {
	mu        sync.Mutex
	rows      map[string]row
	limit     int
	truncated bool
	report    refreshReport
	attempted map[string]bool
}

func newRefreshState(limit int) *refreshState {
	return &refreshState{rows: make(map[string]row), limit: limit, attempted: make(map[string]bool)}
}

// Keep the newest bounded headers. Never silently exceed the IPC body budget.
func (s *refreshState) add(r row) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, pending := s.attempted[r.ID]; pending {
		s.attempted[r.ID] = true // Recovery stays cancelled even if this row is evicted.
	}
	if previous, ok := s.rows[r.ID]; ok && previous.Text != nil && r.Text == nil && r.Revoked == "" {
		return
	}
	s.rows[r.ID] = r
	if len(s.rows) > s.limit {
		oldest := r
		for _, candidate := range s.rows {
			if candidate.TS < oldest.TS || (candidate.TS == oldest.TS && candidate.ID < oldest.ID) {
				oldest = candidate
			}
		}
		delete(s.rows, oldest.ID)
		s.truncated = true
	}
}

func (s *refreshState) live(e *events.Message, aliases map[string]bool) {
	if e == nil || !aliases[e.Info.Chat.String()] {
		return
	}
	s.mu.Lock()
	s.report.Live = min(10000, s.report.Live+1)
	s.mu.Unlock()
	r, ok := messageRow(e.Info.ID, e.Info.Timestamp.Unix(), e.Info.IsFromMe, e.Message, aliases)
	if !ok {
		return
	}
	if e.IsEphemeral || e.IsViewOnce || e.IsViewOnceV2 || e.IsViewOnceV2Extension || e.IsEdit {
		r.Text = nil
	}
	s.add(r)
}

func (s *refreshState) history(data *waHistorySync.HistorySync, aliases map[string]bool) {
	if data == nil || data.GetSyncType() == waHistorySync.HistorySync_ON_DEMAND {
		return
	}
	for _, conv := range data.GetConversations() {
		if !aliases[conv.GetID()] {
			continue
		}
		for _, item := range conv.GetMessages() {
			msg := item.GetMessage()
			key := msg.GetKey()
			if !aliases[key.GetRemoteJID()] {
				continue
			}
			s.mu.Lock()
			s.report.History = min(10000, s.report.History+1)
			s.mu.Unlock()
			r, ok := messageRow(key.GetID(), int64(msg.GetMessageTimestamp()), key.GetFromMe(), msg.GetMessage(), aliases)
			if !ok {
				continue
			}
			if conv.GetEphemeralExpiration() != 0 {
				r.Text = nil
			}
			s.add(r)
		}
	}
}

func (s *refreshState) recovery(e *events.UndecryptableMessage, aliases map[string]bool) bool {
	if e == nil || !aliases[e.Info.Chat.String()] {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.report.Undecryptable = min(10000, s.report.Undecryptable+1)
	// Whatsmeow already asks the phone immediately for an unavailable envelope.
	// Do not duplicate that request, recover view-once/hide payloads, or invent IDs.
	_, attempted := s.attempted[e.Info.ID]
	_, received := s.rows[e.Info.ID]
	if e.IsUnavailable || e.UnavailableType != events.UnavailableTypeUnknown ||
		e.DecryptFailMode != events.DecryptFailShow || !idRE.MatchString(e.Info.ID) ||
		e.Info.Sender.IsEmpty() || e.Info.Timestamp.IsZero() ||
		attempted || received || len(s.attempted) >= 20 {
		return false
	}
	s.attempted[e.Info.ID] = false
	return true
}

func (s *refreshState) result() response {
	s.mu.Lock()
	defer s.mu.Unlock()
	rows := make([]row, 0, len(s.rows))
	for _, r := range s.rows {
		rows = append(rows, r)
	}
	sort.Slice(rows, func(i, j int) bool {
		if rows[i].TS == rows[j].TS {
			return rows[i].ID > rows[j].ID
		}
		return rows[i].TS > rows[j].TS
	})
	// Worst-case JSON escaping is six bytes per text byte. Limit plaintext to
	// 512 KiB, leaving room under the parent's 4 MiB response budget.
	bytes := 0
	for i := range rows {
		if rows[i].Text != nil {
			bytes += len(*rows[i].Text)
			if bytes > 512*1024 {
				rows[i].Text = nil
				rows[i].Truncated = true
				s.truncated = true
			}
		}
	}
	report := s.report
	return response{Status: "refreshed", Rows: rows, Truncated: s.truncated, Refresh: &report}
}

type refreshClient interface {
	historyDownloader
	AddEventHandler(whatsmeow.EventHandler) uint32
	RemoveEventHandler(uint32) bool
	ConnectContext(context.Context) error
	Disconnect()
	IsConnected() bool
	SendPresence(context.Context, types.Presence) error
	SendPeerMessage(context.Context, *waE2E.Message) (whatsmeow.SendResponse, error)
	BuildUnavailableMessageRequest(types.JID, types.JID, string) *waE2E.Message
}

func refresh(ctx context.Context, client refreshClient, r request, aliases map[string]bool) response {
	s := newRefreshState(r.Count)
	connected, stopped := make(chan struct{}, 1), make(chan struct{}, 1)
	work, cancelWork := context.WithCancel(ctx)
	defer cancelWork()
	var workers sync.WaitGroup
	var recoveryMu sync.Mutex
	recoveryClosing := false
	var presenceMu sync.Mutex
	closing := false
	presence := func(value types.Presence, cleanup bool) {
		presenceMu.Lock()
		defer presenceMu.Unlock()
		if closing {
			return
		}
		if cleanup {
			closing = true
		}
		if !client.IsConnected() {
			return
		}
		// Cleanup uses its own short deadline even after the receive budget ends.
		base := work
		if cleanup {
			base = context.Background()
		}
		pctx, cancel := context.WithTimeout(base, 3*time.Second)
		defer cancel()
		err := client.SendPresence(pctx, value)
		s.mu.Lock()
		defer s.mu.Unlock()
		if err != nil {
			s.report.PresenceFailures++
			return
		}
		if value == types.PresenceAvailable {
			s.report.PresenceAvailable = true
		} else {
			s.report.PresenceUnavailable = true
		}
	}
	handler := client.AddEventHandler(func(event any) {
		if work.Err() != nil {
			return
		}
		switch e := event.(type) {
		case *events.Connected:
			select {
			case connected <- struct{}{}:
			default:
			}
			presence(types.PresenceAvailable, false)
		case *events.PushNameSetting:
			presence(types.PresenceAvailable, false)
		case *events.LoggedOut, *events.PairSuccess, *events.Disconnected, *events.StreamReplaced:
			select {
			case stopped <- struct{}{}:
			default:
			}
		case *events.OfflineSyncPreview:
			s.mu.Lock()
			s.report.OfflinePreview = true
			s.mu.Unlock()
		case *events.OfflineSyncCompleted:
			s.mu.Lock()
			s.report.OfflineComplete = true
			s.mu.Unlock()
		case *events.HistorySync:
			s.history(e.Data, aliases)
		case *events.Message:
			notif := e.Message.GetProtocolMessage().GetHistorySyncNotification()
			if notif == nil {
				s.live(e, aliases)
				return
			}
			if notif.GetSyncType() == waE2E.HistorySyncType_ON_DEMAND {
				return
			}
			s.mu.Lock()
			if s.report.HistoryDownloads >= 4 {
				s.truncated = true
				s.mu.Unlock()
				return
			}
			s.report.HistoryDownloads++
			s.mu.Unlock()
			dctx, cancel := context.WithTimeout(work, 10*time.Second)
			data, err := download(dctx, client, notif)
			cancel()
			if err != nil {
				s.mu.Lock()
				s.report.HistoryFailures++
				s.mu.Unlock()
				return
			}
			s.history(data, aliases)
		case *events.UndecryptableMessage:
			recoveryMu.Lock()
			defer recoveryMu.Unlock()
			if recoveryClosing {
				return
			}
			if !s.recovery(e, aliases) {
				return
			}
			info := e.Info
			workers.Add(1)
			go func() {
				defer workers.Done()
				timer := time.NewTimer(5 * time.Second)
				defer timer.Stop()
				select {
				case <-timer.C:
				case <-work.Done():
					return
				}
				s.mu.Lock()
				received := s.attempted[info.ID]
				s.mu.Unlock()
				if received || work.Err() != nil {
					return
				}
				rctx, cancel := context.WithTimeout(work, 3*time.Second)
				_, err := client.SendPeerMessage(rctx, client.BuildUnavailableMessageRequest(info.Chat, info.Sender, info.ID))
				cancel()
				s.mu.Lock()
				defer s.mu.Unlock()
				s.report.RecoveryRequests++
				if err != nil {
					s.report.RecoveryFailures++
				}
			}()
		}
	})
	defer client.Disconnect()
	var shutdownOnce sync.Once
	shutdown := func() {
		shutdownOnce.Do(func() {
			client.RemoveEventHandler(handler)
			recoveryMu.Lock()
			recoveryClosing = true
			recoveryMu.Unlock()
			cancelWork()
			workers.Wait()
			presence(types.PresenceUnavailable, true)
		})
	}
	connectCtx, cancelConnect := context.WithCancel(ctx)
	defer cancelConnect()
	connectTimer := time.AfterFunc(20*time.Second, cancelConnect)
	defer connectTimer.Stop()
	defer shutdown() // Presence cleanup runs before cancelling the socket context.
	if client.ConnectContext(connectCtx) != nil {
		if connectCtx.Err() != nil {
			return response{Error: "connect_timeout"}
		}
		return response{Error: "not_connected"}
	}
	select {
	case <-connected:
	case <-stopped:
		return response{Error: "account_stopped"}
	case <-connectCtx.Done():
		return response{Error: "connect_timeout"}
	}
	connectTimer.Stop()
	if connectCtx.Err() != nil {
		return response{Error: "connect_timeout"}
	}
	select {
	case <-time.After(time.Duration(r.Seconds) * time.Second):
	case <-stopped:
		return response{Error: "account_stopped"}
	case <-ctx.Done():
		return response{Error: "refresh_timeout"}
	}
	// Prevent later callbacks from making the device available after cleanup.
	shutdown()
	return s.result()
}
