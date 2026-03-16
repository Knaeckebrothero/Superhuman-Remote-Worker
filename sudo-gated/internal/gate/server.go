package gate

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"os"
	"sync"
	"time"
)

// Server is the Unix domain socket server that accepts connections from
// the sudo approval plugin and dispatches them to the Handler.
type Server struct {
	socketPath       string
	handler          *Handler
	gracefulTimeout  time.Duration
	log              *slog.Logger

	listener net.Listener
	wg       sync.WaitGroup
}

// ServerConfig holds parameters for creating a Server.
type ServerConfig struct {
	SocketPath      string
	Handler         *Handler
	GracefulTimeout time.Duration
	Logger          *slog.Logger
}

// NewServer creates a Unix socket server.
func NewServer(cfg ServerConfig) *Server {
	return &Server{
		socketPath:      cfg.SocketPath,
		handler:         cfg.Handler,
		gracefulTimeout: cfg.GracefulTimeout,
		log:             cfg.Logger,
	}
}

// Listen creates the Unix domain socket and starts accepting connections.
// The provided context controls the server's lifetime — cancelling it
// initiates a graceful shutdown.
//
// If the socket file already exists (e.g., from a previous crash), it is
// removed before binding. When systemd socket activation is used, the
// socket is inherited from systemd instead.
func (s *Server) Listen(ctx context.Context) error {
	// Check for systemd socket activation.
	if ln, err := s.trySystemdSocket(); err == nil && ln != nil {
		s.listener = ln
		s.log.Info("inherited socket from systemd")
	} else {
		// Clean up stale socket file.
		if _, err := os.Stat(s.socketPath); err == nil {
			s.log.Warn("removing stale socket", "path", s.socketPath)
			if err := os.Remove(s.socketPath); err != nil {
				return fmt.Errorf("remove stale socket: %w", err)
			}
		}

		ln, err := net.Listen("unix", s.socketPath)
		if err != nil {
			return fmt.Errorf("listen %s: %w", s.socketPath, err)
		}

		// Set socket permissions: only root and sudo-gated group.
		if err := os.Chmod(s.socketPath, 0660); err != nil {
			ln.Close()
			return fmt.Errorf("chmod socket: %w", err)
		}

		s.listener = ln
		s.log.Info("listening", "path", s.socketPath)
	}

	// Accept loop.
	go s.acceptLoop(ctx)

	// Wait for context cancellation, then shut down gracefully.
	<-ctx.Done()
	return s.shutdown()
}

// acceptLoop accepts connections until the context is cancelled or the
// listener is closed.
func (s *Server) acceptLoop(ctx context.Context) {
	for {
		conn, err := s.listener.Accept()
		if err != nil {
			// listener.Close() causes Accept to return an error — expected.
			select {
			case <-ctx.Done():
				return
			default:
			}
			s.log.Error("accept", "error", err)
			continue
		}

		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			// Create a child context with the NATS timeout as an upper bound.
			// The handler manages its own internal timeouts (read, NATS request).
			reqCtx, cancel := context.WithTimeout(ctx, s.handler.natsTimeout+10*time.Second)
			defer cancel()
			s.handler.Handle(reqCtx, conn)
		}()
	}
}

// shutdown closes the listener and waits for in-flight requests.
func (s *Server) shutdown() error {
	s.log.Info("shutting down, waiting for in-flight requests", "timeout", s.gracefulTimeout)

	s.listener.Close()

	// Wait for all in-flight requests to finish, with a timeout.
	done := make(chan struct{})
	go func() {
		s.wg.Wait()
		close(done)
	}()

	select {
	case <-done:
		s.log.Info("all requests completed")
	case <-time.After(s.gracefulTimeout):
		s.log.Warn("graceful timeout exceeded, some requests may be abandoned")
	}

	return nil
}

// trySystemdSocket checks if systemd has passed us a socket via
// the LISTEN_FDS protocol (sd_listen_fds). Returns nil, nil if
// no systemd socket is available.
func (s *Server) trySystemdSocket() (net.Listener, error) {
	// systemd sets LISTEN_PID and LISTEN_FDS.
	pid := os.Getenv("LISTEN_PID")
	fds := os.Getenv("LISTEN_FDS")

	if pid == "" || fds == "" || fds == "0" {
		return nil, nil
	}

	if pid != fmt.Sprintf("%d", os.Getpid()) {
		return nil, nil
	}

	// File descriptor 3 is the first socket passed by systemd.
	f := os.NewFile(3, s.socketPath)
	if f == nil {
		return nil, fmt.Errorf("fd 3 not available")
	}

	ln, err := net.FileListener(f)
	f.Close()
	if err != nil {
		return nil, fmt.Errorf("file listener from fd 3: %w", err)
	}

	return ln, nil
}
