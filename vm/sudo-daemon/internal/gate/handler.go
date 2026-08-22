package gate

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"time"

	"golang.org/x/time/rate"

	"github.com/knaeckebrothero/superhuman-remote-worker/sudo-gated/internal/peer"
)

// ApprovalRequest is what the C plugin sends over the Unix socket.
type ApprovalRequest struct {
	Command   string   `json:"command"`
	RunAsUser string   `json:"runas_user"`
	User      string   `json:"user"`
	Host      string   `json:"host"`
	TTY       string   `json:"tty"`
	CWD       string   `json:"cwd"`
	Argv      []string `json:"argv"`
}

// ApprovalResponse is what the daemon sends back to the plugin.
type ApprovalResponse struct {
	Approved bool   `json:"approved"`
	Reason   string `json:"reason,omitempty"`
}

// Handler processes approval requests from the C plugin via Unix socket,
// forwarding them to the configured orchestrator transport.
type Handler struct {
	approver        Approver
	approvalTimeout time.Duration
	vmID            string
	jobID           string
	readTimeout     time.Duration
	limiter         *rate.Limiter
	skipVerify      bool
	log             *slog.Logger
}

// HandlerConfig holds parameters for creating a Handler.
type HandlerConfig struct {
	Approver        Approver
	ApprovalTimeout time.Duration
	VMID            string
	JobID           string
	ReadTimeout     time.Duration
	Limiter         *rate.Limiter
	SkipVerify      bool
	Logger          *slog.Logger
}

// NewHandler creates a request handler.
func NewHandler(cfg HandlerConfig) *Handler {
	return &Handler{
		approver:        cfg.Approver,
		approvalTimeout: cfg.ApprovalTimeout,
		vmID:            cfg.VMID,
		jobID:           cfg.JobID,
		readTimeout:     cfg.ReadTimeout,
		limiter:         cfg.Limiter,
		skipVerify:      cfg.SkipVerify,
		log:             cfg.Logger,
	}
}

// ApprovalTimeout returns the full decision budget used for one invocation.
func (h *Handler) ApprovalTimeout() time.Duration { return h.approvalTimeout }

// Handle processes a single connection from the C plugin.
// It reads the length-prefixed JSON request, forwards it to NATS,
// waits for the reply, and writes the response back.
func (h *Handler) Handle(ctx context.Context, conn net.Conn) {
	defer conn.Close()

	// Verify the connecting process.
	pid, err := peer.Verify(conn, h.skipVerify)
	if err != nil {
		h.log.Warn("peer verification failed", "error", err)
		h.writeResponse(conn, ApprovalResponse{Approved: false, Reason: "peer verification failed"})
		return
	}
	h.log.Debug("connection accepted", "pid", pid)

	// Rate limit.
	if !h.limiter.Allow() {
		h.log.Warn("rate limited", "pid", pid)
		h.writeResponse(conn, ApprovalResponse{Approved: false, Reason: "rate limited"})
		return
	}

	// Read the request with a timeout.
	if err := conn.SetReadDeadline(time.Now().Add(h.readTimeout)); err != nil {
		h.log.Error("set read deadline", "error", err)
		h.writeResponse(conn, ApprovalResponse{Approved: false, Reason: "internal error"})
		return
	}

	req, err := h.readRequest(conn)
	if err != nil {
		h.log.Error("read request", "error", err, "pid", pid)
		h.writeResponse(conn, ApprovalResponse{Approved: false, Reason: "malformed request"})
		return
	}

	// Clear the read deadline — we'll wait for the orchestrator now.
	if err := conn.SetReadDeadline(time.Time{}); err != nil {
		h.log.Error("clear read deadline", "error", err)
	}

	h.log.Info("sudo request",
		"command", req.Command,
		"argv", req.Argv,
		"user", req.User,
		"runas", req.RunAsUser,
		"cwd", req.CWD,
		"pid", pid,
	)

	gateReq := GateRequest{
		ApprovalRequest: *req,
		VMID:            h.vmID,
		JobID:           h.jobID,
		PID:             pid,
	}

	approvalCtx, cancel := context.WithTimeout(ctx, h.approvalTimeout)
	defer cancel()
	resp, err := h.approver.Approve(approvalCtx, gateReq)
	if err != nil {
		if resp.Reason == "" {
			resp.Reason = "approval failed"
		}
		resp.Approved = false
		h.log.Warn("approval request failed", "error", err, "reason", resp.Reason, "command", req.Command)
		h.writeResponse(conn, resp)
		return
	}

	status := "denied"
	if resp.Approved {
		status = "approved"
	}
	h.log.Info("sudo decision",
		"command", req.Command,
		"status", status,
		"reason", resp.Reason,
	)

	h.writeResponse(conn, resp)

	// Brief pause before conn.Close() so the client's poll() sees POLLIN
	// before POLLHUP — on fast Unix sockets the write+close can coalesce
	// into a single event, causing the client to treat POLLHUP as an error.
	time.Sleep(10 * time.Millisecond)
}

// readRequest reads a length-prefixed JSON message from the connection.
//
// Wire format: 4-byte big-endian uint32 (length N) + N bytes of JSON.
func (h *Handler) readRequest(conn net.Conn) (*ApprovalRequest, error) {
	// Read 4-byte length prefix.
	var lengthBuf [4]byte
	if _, err := io.ReadFull(conn, lengthBuf[:]); err != nil {
		return nil, fmt.Errorf("read length prefix: %w", err)
	}

	length := binary.BigEndian.Uint32(lengthBuf[:])

	// Sanity check — reject absurdly large payloads (> 64 KiB).
	const maxPayload = 64 * 1024
	if length > maxPayload {
		return nil, fmt.Errorf("payload too large: %d bytes (max %d)", length, maxPayload)
	}

	// Read the JSON payload.
	payload := make([]byte, length)
	if _, err := io.ReadFull(conn, payload); err != nil {
		return nil, fmt.Errorf("read payload (%d bytes): %w", length, err)
	}

	var req ApprovalRequest
	if err := json.Unmarshal(payload, &req); err != nil {
		return nil, fmt.Errorf("unmarshal request: %w", err)
	}

	return &req, nil
}

// writeResponse writes a length-prefixed JSON response to the connection.
func (h *Handler) writeResponse(conn net.Conn, resp ApprovalResponse) {
	data, err := json.Marshal(resp)
	if err != nil {
		h.log.Error("marshal response", "error", err)
		return
	}

	// Write 4-byte length prefix + JSON payload.
	var lengthBuf [4]byte
	binary.BigEndian.PutUint32(lengthBuf[:], uint32(len(data)))

	if _, err := conn.Write(lengthBuf[:]); err != nil {
		h.log.Error("write length prefix", "error", err)
		return
	}
	if _, err := conn.Write(data); err != nil {
		h.log.Error("write response payload", "error", err)
		return
	}
}
