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

	"github.com/nats-io/nats.go"
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

// NATSRequest is the payload sent to the orchestrator via NATS.
type NATSRequest struct {
	ApprovalRequest
	VMID  string `json:"vm_id"`
	JobID string `json:"job_id"`
	PID   int32  `json:"pid"`
}

// Handler processes approval requests from the C plugin via Unix socket,
// forwarding them to the orchestrator over NATS request/reply.
type Handler struct {
	nc          *nats.Conn
	vmID        string
	jobID       string
	subject     string
	natsTimeout time.Duration
	readTimeout time.Duration
	limiter     *rate.Limiter
	skipVerify  bool
	log         *slog.Logger
}

// HandlerConfig holds parameters for creating a Handler.
type HandlerConfig struct {
	NC          *nats.Conn
	VMID        string
	JobID       string
	Subject     string
	NATSTimeout time.Duration
	ReadTimeout time.Duration
	Limiter     *rate.Limiter
	SkipVerify  bool
	Logger      *slog.Logger
}

// NewHandler creates a request handler.
func NewHandler(cfg HandlerConfig) *Handler {
	return &Handler{
		nc:          cfg.NC,
		vmID:        cfg.VMID,
		jobID:       cfg.JobID,
		subject:     cfg.Subject,
		natsTimeout: cfg.NATSTimeout,
		readTimeout: cfg.ReadTimeout,
		limiter:     cfg.Limiter,
		skipVerify:  cfg.SkipVerify,
		log:         cfg.Logger,
	}
}

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

	// Clear the read deadline — we'll wait for NATS now.
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

	// Build the NATS payload.
	natsReq := NATSRequest{
		ApprovalRequest: *req,
		VMID:            h.vmID,
		JobID:           h.jobID,
		PID:             pid,
	}

	payload, err := json.Marshal(natsReq)
	if err != nil {
		h.log.Error("marshal NATS request", "error", err)
		h.writeResponse(conn, ApprovalResponse{Approved: false, Reason: "internal error"})
		return
	}

	// Forward to orchestrator via NATS request/reply.
	// Use a dedicated timeout context for the NATS request (300s default).
	subject := fmt.Sprintf("%s.%s.%s", h.subject, h.vmID, h.jobID)
	h.log.Debug("forwarding to NATS", "subject", subject)

	natsCtx, natsCancel := context.WithTimeout(ctx, h.natsTimeout)
	defer natsCancel()

	msg, err := h.nc.RequestWithContext(natsCtx, subject, payload)
	if err != nil {
		reason := "orchestrator unreachable"
		if err == nats.ErrTimeout || err == context.DeadlineExceeded {
			reason = "approval timed out"
		} else if err == nats.ErrNoResponders {
			reason = "no orchestrator listening"
		} else if err == context.Canceled {
			reason = "daemon shutting down"
		}
		h.log.Warn("NATS request failed", "error", err, "reason", reason, "command", req.Command)
		h.writeResponse(conn, ApprovalResponse{Approved: false, Reason: reason})
		return
	}

	// Parse the orchestrator's reply.
	var resp ApprovalResponse
	if err := json.Unmarshal(msg.Data, &resp); err != nil {
		h.log.Error("unmarshal NATS response", "error", err, "data", string(msg.Data))
		h.writeResponse(conn, ApprovalResponse{Approved: false, Reason: "malformed orchestrator response"})
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
