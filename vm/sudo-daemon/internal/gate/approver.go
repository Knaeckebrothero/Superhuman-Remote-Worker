package gate

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	mathrand "math/rand"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"
)

const (
	defaultHTTPAttemptTimeout = 35 * time.Second
	defaultHTTPRetryBase      = time.Second
	defaultHTTPRetryMax       = 10 * time.Second
)

// GateRequest is the request forwarded to an approval transport.
// RequestID is populated only by the HTTP approver, so the NATS payload stays
// byte-for-byte compatible with the existing request/reply protocol.
type GateRequest struct {
	ApprovalRequest
	RequestID string `json:"request_id,omitempty"`
	VMID      string `json:"vm_id"`
	JobID     string `json:"job_id"`
	PID       int32  `json:"pid"`
}

// Approver decides whether a sudo request may proceed.
type Approver interface {
	Approve(ctx context.Context, req GateRequest) (ApprovalResponse, error)
}

// ApproverError carries the fail-closed reason returned to the sudo plugin.
type ApproverError struct {
	Reason string
	Err    error
}

func (e *ApproverError) Error() string {
	if e.Err == nil {
		return e.Reason
	}
	if e.Err.Error() == e.Reason {
		return e.Reason
	}
	return fmt.Sprintf("%s: %v", e.Reason, e.Err)
}

func (e *ApproverError) Unwrap() error { return e.Err }

func denied(reason string, err error) (ApprovalResponse, error) {
	if reason == "" {
		reason = "approval failed"
	}
	return ApprovalResponse{Approved: false, Reason: reason}, &ApproverError{Reason: reason, Err: err}
}

// NATSApprover implements the existing NATS request/reply transport.
type NATSApprover struct {
	nc            *nats.Conn
	subjectPrefix string
}

// NewNATSApprover creates an approver backed by NATS request/reply.
func NewNATSApprover(nc *nats.Conn, subjectPrefix string) *NATSApprover {
	return &NATSApprover{nc: nc, subjectPrefix: subjectPrefix}
}

// Approve forwards a request using the legacy NATS wire protocol.
func (a *NATSApprover) Approve(ctx context.Context, req GateRequest) (ApprovalResponse, error) {
	payload, err := json.Marshal(req)
	if err != nil {
		return denied("internal error", err)
	}

	subject := fmt.Sprintf("%s.%s.%s", a.subjectPrefix, req.VMID, req.JobID)
	msg, err := a.nc.RequestWithContext(ctx, subject, payload)
	if err != nil {
		reason := "orchestrator unreachable"
		switch {
		case errors.Is(err, nats.ErrTimeout), errors.Is(err, context.DeadlineExceeded):
			reason = "approval timed out"
		case errors.Is(err, nats.ErrNoResponders):
			reason = "no orchestrator listening"
		case errors.Is(err, context.Canceled):
			reason = "daemon shutting down"
		}
		return denied(reason, err)
	}

	var resp ApprovalResponse
	if err := json.Unmarshal(msg.Data, &resp); err != nil {
		return denied("malformed orchestrator response", err)
	}
	if !resp.Approved && resp.Reason == "" {
		resp.Reason = "denied by orchestrator"
	}
	return resp, nil
}

// HTTPApprover implements create plus bounded long-poll over HTTP.
type HTTPApprover struct {
	client       *http.Client
	baseURL      string
	token        string
	entityID     string
	attemptLimit time.Duration
	retryBase    time.Duration
	retryMax     time.Duration
	pollFloor    time.Duration
	jitter       func(time.Duration) time.Duration
	log          *slog.Logger
}

// NewHTTPApprover creates an HTTP approver. The client must not set a global
// Timeout; each request receives its own context deadline instead.
func NewHTTPApprover(client *http.Client, baseURL, token, entityID string) *HTTPApprover {
	return &HTTPApprover{
		client:       client,
		baseURL:      strings.TrimRight(baseURL, "/"),
		token:        token,
		entityID:     entityID,
		attemptLimit: defaultHTTPAttemptTimeout,
		retryBase:    defaultHTTPRetryBase,
		retryMax:     defaultHTTPRetryMax,
		pollFloor:    time.Second,
		jitter: func(d time.Duration) time.Duration {
			return time.Duration(float64(d) * (0.8 + mathrand.Float64()*0.4))
		},
		log: slog.Default(),
	}
}

// WithLogger uses logger for transport diagnostics. Tokens are never logged.
func (a *HTTPApprover) WithLogger(logger *slog.Logger) *HTTPApprover {
	if logger != nil {
		a.log = logger
	}
	return a
}

type httpSudoRequest struct {
	RequestID string   `json:"request_id"`
	Command   string   `json:"command"`
	Argv      []string `json:"argv"`
	RunAsUser string   `json:"runas_user"`
	User      string   `json:"user"`
	Host      string   `json:"host"`
	TTY       string   `json:"tty"`
	CWD       string   `json:"cwd"`
	PID       int32    `json:"pid"`
}

type httpDecision struct {
	RequestID string  `json:"request_id"`
	Status    string  `json:"status"`
	Reason    *string `json:"reason"`
	ExpiresAt string  `json:"expires_at"`
}

// Approve creates one idempotent request and polls until an explicit decision.
func (a *HTTPApprover) Approve(ctx context.Context, req GateRequest) (ApprovalResponse, error) {
	requestID, err := newUUID4()
	if err != nil {
		return denied("internal error", err)
	}
	req.RequestID = requestID

	body := httpSudoRequest{
		RequestID: requestID,
		Command:   req.Command,
		Argv:      req.Argv,
		RunAsUser: req.RunAsUser,
		User:      req.User,
		Host:      req.Host,
		TTY:       req.TTY,
		CWD:       req.CWD,
		PID:       req.PID,
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return denied("internal error", err)
	}

	createURL := fmt.Sprintf("%s/api/internal/vm/%s/sudo", a.baseURL, url.PathEscape(a.entityID))
	decision, err := a.retryRequest(ctx, http.MethodPost, createURL, payload, requestID)
	if err != nil {
		return a.failure(ctx, err)
	}
	if response, done, err := decisionResult(requestID, decision); done {
		return response, err
	}

	pollURL := fmt.Sprintf("%s/%s?wait=25", createURL, url.PathEscape(requestID))
	for {
		decision, err = a.retryRequest(ctx, http.MethodGet, pollURL, nil, "")
		if err != nil {
			return a.failure(ctx, err)
		}
		if response, done, err := decisionResult(requestID, decision); done {
			return response, err
		}
	}
}

func (a *HTTPApprover) failure(ctx context.Context, err error) (ApprovalResponse, error) {
	if errors.Is(ctx.Err(), context.DeadlineExceeded) || errors.Is(err, context.DeadlineExceeded) {
		return denied("approval timed out", err)
	}
	if errors.Is(ctx.Err(), context.Canceled) || errors.Is(err, context.Canceled) {
		return denied("daemon shutting down", err)
	}
	var fatal *httpFatalError
	if errors.As(err, &fatal) {
		return denied(fatal.reason, err)
	}
	return denied("orchestrator unreachable", err)
}

func decisionResult(requestID string, decision httpDecision) (ApprovalResponse, bool, error) {
	if decision.RequestID != requestID {
		resp, err := denied("malformed orchestrator response: request_id mismatch", nil)
		return resp, true, err
	}
	reason := ""
	if decision.Reason != nil {
		reason = *decision.Reason
	}
	switch decision.Status {
	case "pending":
		return ApprovalResponse{}, false, nil
	case "approved":
		return ApprovalResponse{Approved: true, Reason: reason}, true, nil
	case "denied", "expired":
		if reason == "" {
			reason = "approval " + decision.Status
		}
		return ApprovalResponse{Approved: false, Reason: reason}, true, nil
	default:
		resp, err := denied("malformed orchestrator response: invalid status", nil)
		return resp, true, err
	}
}

type httpFatalError struct {
	reason string
	err    error
}

func (e *httpFatalError) Error() string { return e.reason }
func (e *httpFatalError) Unwrap() error { return e.err }

func (a *HTTPApprover) retryRequest(
	ctx context.Context,
	method string,
	requestURL string,
	payload []byte,
	idempotencyKey string,
) (httpDecision, error) {
	backoff := a.retryBase
	for {
		started := time.Now()
		decision, retryAfter, retryable, err := a.doRequest(ctx, method, requestURL, payload, idempotencyKey)
		elapsed := time.Since(started)
		if err == nil {
			if decision.Status == "pending" && elapsed < a.pollFloor {
				if err := sleepContext(ctx, a.pollFloor); err != nil {
					return httpDecision{}, err
				}
			}
			return decision, nil
		}
		if !retryable {
			return httpDecision{}, err
		}

		delay := retryAfter
		if delay < 0 {
			delay = a.jitter(backoff)
		}
		if elapsed < a.pollFloor {
			delay = maxDuration(delay, a.pollFloor)
		}
		if err := sleepContext(ctx, delay); err != nil {
			return httpDecision{}, err
		}
		backoff = minDuration(a.retryMax, backoff*2)
	}
}

func (a *HTTPApprover) doRequest(
	ctx context.Context,
	method string,
	requestURL string,
	payload []byte,
	idempotencyKey string,
) (httpDecision, time.Duration, bool, error) {
	attemptCtx, cancel := context.WithTimeout(ctx, a.attemptLimit)
	defer cancel()

	var body io.Reader
	if payload != nil {
		body = bytes.NewReader(payload)
	}
	req, err := http.NewRequestWithContext(attemptCtx, method, requestURL, body)
	if err != nil {
		return httpDecision{}, -1, false, &httpFatalError{reason: "invalid orchestrator URL", err: err}
	}
	req.Header.Set("Authorization", "Bearer "+a.token)
	req.Header.Set("Content-Type", "application/json")
	if idempotencyKey != "" {
		req.Header.Set("Idempotency-Key", idempotencyKey)
	}

	resp, err := a.client.Do(req)
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) || isNetworkError(err) {
			return httpDecision{}, -1, true, err
		}
		return httpDecision{}, -1, false, &httpFatalError{
			reason: "non-retryable HTTP transport error",
			err:    err,
		}
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode == http.StatusServiceUnavailable {
		return httpDecision{}, parseRetryAfter(resp.Header.Get("Retry-After")), true,
			fmt.Errorf("orchestrator returned HTTP %d", resp.StatusCode)
	}
	if resp.StatusCode == http.StatusConflict {
		reason := "orchestrator rejected request (HTTP 409)"
		if method == http.MethodPost {
			reason = "request_id conflict"
		}
		return httpDecision{}, -1, false, &httpFatalError{reason: reason}
	}
	if resp.StatusCode >= 500 {
		return httpDecision{}, -1, true, fmt.Errorf("orchestrator returned HTTP %d", resp.StatusCode)
	}
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		reason := fmt.Sprintf("orchestrator rejected request (HTTP %d)", resp.StatusCode)
		return httpDecision{}, -1, false, &httpFatalError{reason: reason}
	}

	responseBody, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		return httpDecision{}, -1, false, &httpFatalError{reason: "malformed orchestrator response", err: err}
	}
	var decision httpDecision
	if err := json.Unmarshal(responseBody, &decision); err != nil {
		preview := responseBody
		if len(preview) > 256 {
			preview = preview[:256]
		}
		previewText := string(preview)
		if a.token != "" {
			previewText = strings.ReplaceAll(previewText, a.token, "[REDACTED]")
		}
		a.log.Debug("unparseable orchestrator response",
			"body_preview", previewText,
			"body_truncated", len(responseBody) > len(preview),
			"error", err,
		)
		return httpDecision{}, -1, false, &httpFatalError{reason: "malformed orchestrator response", err: err}
	}
	return decision, -1, false, nil
}

func newUUID4() (string, error) {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", err
	}
	raw[6] = (raw[6] & 0x0f) | 0x40
	raw[8] = (raw[8] & 0x3f) | 0x80
	encoded := make([]byte, 36)
	hex.Encode(encoded[0:8], raw[0:4])
	encoded[8] = '-'
	hex.Encode(encoded[9:13], raw[4:6])
	encoded[13] = '-'
	hex.Encode(encoded[14:18], raw[6:8])
	encoded[18] = '-'
	hex.Encode(encoded[19:23], raw[8:10])
	encoded[23] = '-'
	hex.Encode(encoded[24:36], raw[10:16])
	return string(encoded), nil
}

func parseRetryAfter(value string) time.Duration {
	seconds, err := strconv.Atoi(strings.TrimSpace(value))
	if err != nil || seconds < 0 {
		return -1
	}
	return time.Duration(seconds) * time.Second
}

func isNetworkError(err error) bool {
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return true
	}
	var opErr *net.OpError
	return errors.As(err, &opErr) ||
		errors.Is(err, syscall.ECONNREFUSED) ||
		errors.Is(err, syscall.ECONNRESET) ||
		errors.Is(err, io.EOF) ||
		errors.Is(err, io.ErrUnexpectedEOF)
}

func sleepContext(ctx context.Context, delay time.Duration) error {
	if delay <= 0 {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
			return nil
		}
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func minDuration(a, b time.Duration) time.Duration {
	return time.Duration(math.Min(float64(a), float64(b)))
}

func maxDuration(a, b time.Duration) time.Duration {
	if a > b {
		return a
	}
	return b
}
