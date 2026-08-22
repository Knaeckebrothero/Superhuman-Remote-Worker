package gate

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func testGateRequest() GateRequest {
	return GateRequest{
		ApprovalRequest: ApprovalRequest{
			Command:   "/usr/bin/apt-get",
			Argv:      []string{"apt-get", "install", "curl"},
			RunAsUser: "root",
			User:      "agent-host",
			Host:      "test-vm",
			TTY:       "/dev/pts/1",
			CWD:       "/workspace",
		},
		VMID:  "agent-vm-entity-1",
		JobID: "entity-1",
		PID:   1234,
	}
}

type handlerTransport struct{ handler http.Handler }

func (t handlerTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if err := req.Context().Err(); err != nil {
		return nil, err
	}
	recorder := httptest.NewRecorder()
	t.handler.ServeHTTP(recorder, req)
	return recorder.Result(), nil
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func testHTTPApprover(handler http.Handler) *HTTPApprover {
	client := &http.Client{Transport: handlerTransport{handler: handler}}
	a := NewHTTPApprover(client, "http://orchestrator.test", strings.Repeat("a", 64), "entity-1")
	a.retryBase = time.Millisecond
	a.retryMax = 2 * time.Millisecond
	a.attemptLimit = 50 * time.Millisecond
	a.pollFloor = time.Millisecond
	a.jitter = func(d time.Duration) time.Duration { return d }
	return a
}

func testNetworkHTTPApprover(serverURL string) *HTTPApprover {
	a := NewHTTPApprover(&http.Client{}, serverURL, strings.Repeat("a", 64), "entity-1")
	a.retryBase = time.Millisecond
	a.retryMax = 2 * time.Millisecond
	a.attemptLimit = 50 * time.Millisecond
	a.pollFloor = time.Millisecond
	a.jitter = func(d time.Duration) time.Duration { return d }
	return a
}

func writeDecision(t *testing.T, w http.ResponseWriter, statusCode int, requestID, status string, reason any) {
	t.Helper()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	if err := json.NewEncoder(w).Encode(map[string]any{
		"request_id": requestID,
		"status":     status,
		"reason":     reason,
		"expires_at": "2099-01-01T00:00:00Z",
	}); err != nil {
		t.Fatalf("encode response: %v", err)
	}
}

func assertDenied(t *testing.T, resp ApprovalResponse, err error) {
	t.Helper()
	if resp.Approved {
		t.Fatal("failure path unexpectedly approved request")
	}
	if resp.Reason == "" {
		t.Fatal("failure path returned an empty reason")
	}
	if err == nil {
		t.Fatal("transport/protocol failure returned no error")
	}
}

func TestHTTPApproverApproved(t *testing.T) {
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Fatalf("method = %s, want POST", r.Method)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer "+strings.Repeat("a", 64) {
			t.Errorf("Authorization = %q", got)
		}
		if got := r.Header.Get("Content-Type"); got != "application/json" {
			t.Errorf("Content-Type = %q", got)
		}
		var body httpSudoRequest
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		if body.RequestID == "" || r.Header.Get("Idempotency-Key") != body.RequestID {
			t.Errorf("idempotency key %q does not match request ID %q", r.Header.Get("Idempotency-Key"), body.RequestID)
		}
		if body.Command != "/usr/bin/apt-get" || body.PID != 1234 {
			t.Errorf("unexpected request: %+v", body)
		}
		writeDecision(t, w, http.StatusCreated, body.RequestID, "approved", nil)
	})

	resp, err := testHTTPApprover(handler).Approve(context.Background(), testGateRequest())
	if err != nil || !resp.Approved {
		t.Fatalf("Approve() = %+v, %v", resp, err)
	}
}

func TestHTTPApproverDenied(t *testing.T) {
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body httpSudoRequest
		_ = json.NewDecoder(r.Body).Decode(&body)
		writeDecision(t, w, http.StatusCreated, body.RequestID, "denied", "operator denied")
	})

	resp, err := testHTTPApprover(handler).Approve(context.Background(), testGateRequest())
	if err != nil || resp.Approved || resp.Reason != "operator denied" {
		t.Fatalf("Approve() = %+v, %v", resp, err)
	}
}

func TestHTTPApproverRejectsMismatchedRequestID(t *testing.T) {
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeDecision(t, w, http.StatusCreated, "different-request-id", "approved", nil)
	})

	resp, err := testHTTPApprover(handler).Approve(context.Background(), testGateRequest())
	assertDenied(t, resp, err)
	if !strings.Contains(resp.Reason, "request_id mismatch") {
		t.Fatalf("reason = %q", resp.Reason)
	}
}

func TestHTTPApproverExpired(t *testing.T) {
	var requestID string
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			var body httpSudoRequest
			_ = json.NewDecoder(r.Body).Decode(&body)
			requestID = body.RequestID
			writeDecision(t, w, http.StatusCreated, requestID, "pending", nil)
			return
		}
		writeDecision(t, w, http.StatusOK, requestID, "expired", nil)
	})

	resp, err := testHTTPApprover(handler).Approve(context.Background(), testGateRequest())
	if err != nil || resp.Approved || resp.Reason == "" {
		t.Fatalf("Approve() = %+v, %v", resp, err)
	}
}

func TestHTTPApproverPendingThenApproved(t *testing.T) {
	var requestID string
	var polls atomic.Int32
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			var body httpSudoRequest
			_ = json.NewDecoder(r.Body).Decode(&body)
			requestID = body.RequestID
			writeDecision(t, w, http.StatusCreated, requestID, "pending", nil)
			return
		}
		if r.URL.Query().Get("wait") != "25" {
			t.Errorf("wait = %q", r.URL.Query().Get("wait"))
		}
		if r.Header.Get("Authorization") == "" || r.Header.Get("Content-Type") != "application/json" {
			t.Error("poll omitted required headers")
		}
		if polls.Add(1) < 4 {
			writeDecision(t, w, http.StatusOK, requestID, "pending", nil)
			return
		}
		writeDecision(t, w, http.StatusOK, requestID, "approved", nil)
	})

	resp, err := testHTTPApprover(handler).Approve(context.Background(), testGateRequest())
	if err != nil || !resp.Approved || polls.Load() != 4 {
		t.Fatalf("Approve() = %+v, %v; polls=%d", resp, err, polls.Load())
	}
}

func TestHTTPApproverBudgetExhaustion(t *testing.T) {
	var requestID string
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		status := http.StatusOK
		if r.Method == http.MethodPost {
			status = http.StatusCreated
			var body httpSudoRequest
			_ = json.NewDecoder(r.Body).Decode(&body)
			requestID = body.RequestID
		}
		writeDecision(t, w, status, requestID, "pending", nil)
	})

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Millisecond)
	defer cancel()
	resp, err := testHTTPApprover(handler).Approve(ctx, testGateRequest())
	assertDenied(t, resp, err)
	if resp.Reason != "approval timed out" {
		t.Fatalf("reason = %q", resp.Reason)
	}
}

func TestHTTPApproverFatalStatuses(t *testing.T) {
	for _, status := range []int{http.StatusUnauthorized, http.StatusNotFound, http.StatusUnprocessableEntity} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(status)
			})

			resp, err := testHTTPApprover(handler).Approve(context.Background(), testGateRequest())
			assertDenied(t, resp, err)
			if !strings.Contains(resp.Reason, strconv.Itoa(status)) {
				t.Fatalf("reason %q does not name HTTP %d", resp.Reason, status)
			}
		})
	}
}

func TestHTTPApproverRetries5xxThenSucceeds(t *testing.T) {
	var attempts atomic.Int32
	var bodies [][]byte
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		bodyBytes, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("read POST body: %v", err)
		}
		bodies = append(bodies, bodyBytes)
		if attempts.Add(1) < 3 {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		var body httpSudoRequest
		if err := json.Unmarshal(bodyBytes, &body); err != nil {
			t.Fatalf("decode POST body: %v", err)
		}
		writeDecision(t, w, http.StatusCreated, body.RequestID, "approved", nil)
	})

	resp, err := testHTTPApprover(handler).Approve(context.Background(), testGateRequest())
	if err != nil || !resp.Approved || attempts.Load() != 3 {
		t.Fatalf("Approve() = %+v, %v; attempts=%d", resp, err, attempts.Load())
	}
	if len(bodies) != 3 || !bytes.Equal(bodies[0], bodies[1]) || !bytes.Equal(bodies[1], bodies[2]) {
		t.Fatalf("POST bodies changed across retries: %q", bodies)
	}
	var first, last httpSudoRequest
	if err := json.Unmarshal(bodies[0], &first); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(bodies[2], &last); err != nil {
		t.Fatal(err)
	}
	if first.RequestID == "" || first.RequestID != last.RequestID {
		t.Fatalf("request_id changed across retries: %q != %q", first.RequestID, last.RequestID)
	}
}

func TestHTTPApproverTreatsCreateConflictAsFatal(t *testing.T) {
	var attempts atomic.Int32
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempts.Add(1)
		w.WriteHeader(http.StatusConflict)
	})

	resp, err := testHTTPApprover(handler).Approve(context.Background(), testGateRequest())
	assertDenied(t, resp, err)
	if resp.Reason != "request_id conflict" {
		t.Fatalf("reason = %q", resp.Reason)
	}
	if attempts.Load() != 1 {
		t.Fatalf("409 was retried %d times", attempts.Load())
	}
}

func TestHTTPApproverHonorsRetryAfter(t *testing.T) {
	var attempts atomic.Int32
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if attempts.Add(1) == 1 {
			w.Header().Set("Retry-After", "0")
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		var body httpSudoRequest
		_ = json.NewDecoder(r.Body).Decode(&body)
		writeDecision(t, w, http.StatusCreated, body.RequestID, "approved", nil)
	})

	a := testHTTPApprover(handler)
	a.retryBase = time.Second
	a.pollFloor = 10 * time.Millisecond
	started := time.Now()
	resp, err := a.Approve(context.Background(), testGateRequest())
	if err != nil || !resp.Approved || attempts.Load() != 2 {
		t.Fatalf("Approve() = %+v, %v; attempts=%d", resp, err, attempts.Load())
	}
	if elapsed := time.Since(started); elapsed < a.pollFloor || elapsed >= 500*time.Millisecond {
		t.Fatalf("Retry-After: 0 poll floor not honored; elapsed=%s", elapsed)
	}
}

func TestHTTPApproverConnectionRefused(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Millisecond)
	defer cancel()
	resp, err := testNetworkHTTPApprover("http://127.0.0.1:1").Approve(ctx, testGateRequest())
	assertDenied(t, resp, err)
}

func TestHTTPApproverMalformedJSON(t *testing.T) {
	malformedBody := strings.Repeat("x", 300)
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(malformedBody))
	})

	var logs bytes.Buffer
	a := testHTTPApprover(handler).WithLogger(slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{
		Level: slog.LevelDebug,
	})))
	resp, err := a.Approve(context.Background(), testGateRequest())
	assertDenied(t, resp, err)
	if !strings.Contains(resp.Reason, "malformed") {
		t.Fatalf("reason = %q", resp.Reason)
	}
	if !strings.Contains(logs.String(), strings.Repeat("x", 256)) || strings.Contains(logs.String(), strings.Repeat("x", 257)) {
		t.Fatalf("debug preview was not capped at 256 bytes: %s", logs.String())
	}
}

func TestHTTPApproverMalformedJSONLogRedactsToken(t *testing.T) {
	token := strings.Repeat("a", 64)
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte("malformed " + token))
	})

	var logs bytes.Buffer
	a := testHTTPApprover(handler).WithLogger(slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{
		Level: slog.LevelDebug,
	})))
	resp, err := a.Approve(context.Background(), testGateRequest())
	assertDenied(t, resp, err)
	if strings.Contains(logs.String(), token) || !strings.Contains(logs.String(), "[REDACTED]") {
		t.Fatalf("debug response preview exposed the bearer token: %s", logs.String())
	}
}

func TestHTTPApproverFailsFastOnNonNetworkTransportError(t *testing.T) {
	var attempts atomic.Int32
	client := &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		attempts.Add(1)
		return nil, &url.Error{Op: "Get", URL: "https://example.invalid", Err: errors.New("redirect forbidden")}
	})}
	a := NewHTTPApprover(client, "https://example.invalid", strings.Repeat("a", 64), "entity-1")
	a.pollFloor = time.Millisecond

	resp, err := a.Approve(context.Background(), testGateRequest())
	assertDenied(t, resp, err)
	if resp.Reason != "non-retryable HTTP transport error" {
		t.Fatalf("reason = %q", resp.Reason)
	}
	if attempts.Load() != 1 {
		t.Fatalf("fatal transport error was retried %d times", attempts.Load())
	}
}

func TestIsNetworkErrorClassification(t *testing.T) {
	if isNetworkError(&url.Error{Op: "Get", URL: "https://example.invalid", Err: errors.New("redirect forbidden")}) {
		t.Fatal("generic url.Error must be fatal")
	}
	if !isNetworkError(&net.OpError{Op: "dial", Net: "tcp", Err: errors.New("dial failed")}) {
		t.Fatal("net.OpError must be retryable")
	}
	if !isNetworkError(io.ErrUnexpectedEOF) {
		t.Fatal("unexpected EOF must be retryable")
	}
}

func TestApproverErrorDeduplicatesReason(t *testing.T) {
	err := &ApproverError{
		Reason: "request_id conflict",
		Err:    &httpFatalError{reason: "request_id conflict"},
	}
	if got := err.Error(); got != "request_id conflict" {
		t.Fatalf("Error() = %q", got)
	}
}
