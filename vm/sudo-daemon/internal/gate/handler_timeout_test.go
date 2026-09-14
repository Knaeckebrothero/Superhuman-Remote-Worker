package gate

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/superhuman-remote-worker/srw/sudo-gated/internal/config"
)

// Exercise the production handler framing, HTTP transport, and loaded budgets.
// One production second takes 200 microseconds here: ten minutes takes 120ms.
func TestShippedBudgetsDeliverTenMinuteApproval(t *testing.T) {
	for _, path := range []string{"", "../../config.example.yaml", "../../../../docker/agent-vm-base/files/sudo-gated-config.yaml"} {
		t.Run(path, func(t *testing.T) {
			cfg, err := config.Load(path)
			if err != nil {
				t.Fatal(err)
			}
			budget := cfg.Timeouts.NATSRequest / 5000
			response := runDelayedHTTPDecision(t, budget, 120*time.Millisecond, "approved")
			if !response.Approved {
				t.Fatalf("ten-minute approval was lost: %+v", response)
			}
		})
	}
}

func TestConfiguredBudgetAllowsExpiryAndStillBoundsUnansweredRequests(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte("timeouts:\n  nats_request: 150ms\n"), 0600); err != nil {
		t.Fatal(err)
	}
	cfg, err := config.Load(path)
	if err != nil {
		t.Fatal(err)
	}
	expired := runDelayedHTTPDecision(t, cfg.Timeouts.NATSRequest, 30*time.Millisecond, "expired")
	if expired.Approved || expired.Reason != "approval expired" {
		t.Fatalf("shorter server TTL must remain authoritative: %+v", expired)
	}
	unanswered := runDelayedHTTPDecision(t, cfg.Timeouts.NATSRequest, time.Second, "approved")
	if unanswered.Approved || unanswered.Reason != "approval timed out" {
		t.Fatalf("transport must fail closed at its configured deadline: %+v", unanswered)
	}
}

func runDelayedHTTPDecision(t *testing.T, budget, delay time.Duration, status string) ApprovalResponse {
	t.Helper()
	var requestID string
	approver := testHTTPApprover(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			var body httpSudoRequest
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
				return
			}
			requestID = body.RequestID
			writeDecision(t, w, http.StatusCreated, requestID, "pending", nil)
			return
		}
		timer := time.NewTimer(delay)
		defer timer.Stop()
		select {
		case <-r.Context().Done():
			return
		case <-timer.C:
			writeDecision(t, w, http.StatusOK, requestID, status, nil)
		}
	}))
	approver.attemptLimit = 2 * time.Second
	handler := NewHandler(HandlerConfig{
		Approver: approver, ApprovalTimeout: budget, ReadTimeout: time.Second,
		VMID: "vm-test", JobID: "entity-1", Limiter: NewRateLimiter(5, 3),
		SkipVerify: true, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	// Real Unix sockets retain peer credential checks; only executable identity
	// is skipped because the peer is this test process rather than sudo.
	dir, err := os.MkdirTemp("", "sudo-timeout-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: filepath.Join(dir, "gate.sock"), Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	done := make(chan struct{})
	go func() {
		defer close(done)
		conn, err := listener.AcceptUnix()
		if err != nil {
			t.Error(err)
			return
		}
		handler.Handle(context.Background(), conn)
	}()
	conn, err := net.DialUnix("unix", nil, listener.Addr().(*net.UnixAddr))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
		t.Fatal(err)
	}
	payload, err := json.Marshal(testGateRequest().ApprovalRequest)
	if err != nil {
		t.Fatal(err)
	}
	if err := binary.Write(conn, binary.BigEndian, uint32(len(payload))); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Write(payload); err != nil {
		t.Fatal(err)
	}
	var length uint32
	if err := binary.Read(conn, binary.BigEndian, &length); err != nil {
		t.Fatal(err)
	}
	if length > 65536 {
		t.Fatalf("oversized response: %d", length)
	}
	data := make([]byte, length)
	if _, err := io.ReadFull(conn, data); err != nil {
		t.Fatal(err)
	}
	var response ApprovalResponse
	if err := json.Unmarshal(data, &response); err != nil {
		t.Fatal(err)
	}
	<-done
	return response
}
