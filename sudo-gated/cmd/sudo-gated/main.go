// sudo-gated is the sudo approval gate daemon.
//
// It listens on a Unix domain socket for requests from the sudo approval
// plugin (sudo_gate.so), forwards them to the orchestrator via NATS
// request/reply, and relays the approval/denial decision back.
//
// Configuration is loaded from a YAML file (--config flag) with environment
// variable overrides (NATS_URL, JOB_ID, VM_ID).
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/knaeckebrothero/superhuman-remote-worker/sudo-gated/internal/config"
	"github.com/knaeckebrothero/superhuman-remote-worker/sudo-gated/internal/gate"
)

var version = "dev"

func main() {
	configPath := flag.String("config", "", "path to config YAML (optional)")
	showVersion := flag.Bool("version", false, "print version and exit")
	skipVerify := flag.Bool("skip-verify", false, "skip SO_PEERCRED binary verification (dev only)")
	flag.Parse()

	if *showVersion {
		fmt.Println("sudo-gated", version)
		os.Exit(0)
	}

	// Load configuration.
	cfg, err := config.Load(*configPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "config: %v\n", err)
		os.Exit(1)
	}

	// Set up structured logging.
	var logLevel slog.Level
	switch cfg.LogLevel {
	case "debug":
		logLevel = slog.LevelDebug
	case "warn":
		logLevel = slog.LevelWarn
	case "error":
		logLevel = slog.LevelError
	default:
		logLevel = slog.LevelInfo
	}

	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{
		Level: logLevel,
	}))
	logger = logger.With("component", "sudo-gated", "version", version)

	logger.Info("starting",
		"socket", cfg.SocketPath,
		"nats", cfg.NATS.URL,
		"job_id", cfg.JobID,
		"vm_id", cfg.VMID,
	)

	if cfg.JobID == "" {
		logger.Warn("JOB_ID not set — NATS subject will use empty job_id")
	}

	// Connect to NATS.
	nc, err := nats.Connect(cfg.NATS.URL,
		nats.MaxReconnects(cfg.NATS.MaxReconnects),
		nats.ReconnectWait(cfg.NATS.ReconnectWait),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			logger.Warn("NATS disconnected", "error", err)
		}),
		nats.ReconnectHandler(func(_ *nats.Conn) {
			logger.Info("NATS reconnected")
		}),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) {
			logger.Error("NATS error", "error", err)
		}),
	)
	if err != nil {
		logger.Error("NATS connect failed", "error", err, "url", cfg.NATS.URL)
		os.Exit(1)
	}
	defer nc.Close()
	logger.Info("NATS connected", "url", cfg.NATS.URL)

	// Build the handler.
	limiter := gate.NewRateLimiter(cfg.RateLimit.RequestsPerMinute, cfg.RateLimit.Burst)

	handler := gate.NewHandler(gate.HandlerConfig{
		NC:          nc,
		VMID:        cfg.VMID,
		JobID:       cfg.JobID,
		Subject:     cfg.NATS.SubjectPrefix,
		NATSTimeout: cfg.Timeouts.NATSRequest,
		ReadTimeout: cfg.Timeouts.SocketRead,
		Limiter:     limiter,
		SkipVerify:  *skipVerify,
		Logger:      logger.With("component", "handler"),
	})

	// Build the server.
	server := gate.NewServer(gate.ServerConfig{
		SocketPath:      cfg.SocketPath,
		Handler:         handler,
		GracefulTimeout: cfg.Timeouts.GracefulShutdown,
		Logger:          logger.With("component", "server"),
	})

	// Context cancelled by SIGTERM/SIGINT.
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()

	// Notify systemd that we're ready (sd_notify protocol).
	sdNotify("READY=1")

	// Start watchdog pinger if WatchdogSec is configured.
	go watchdogLoop(ctx, logger)

	// Run until shutdown.
	if err := server.Listen(ctx); err != nil {
		logger.Error("server error", "error", err)
		os.Exit(1)
	}

	sdNotify("STOPPING=1")
	logger.Info("stopped")
}

// watchdogLoop sends WATCHDOG=1 to systemd at half the configured interval.
// systemd sets WATCHDOG_USEC when WatchdogSec is configured in the unit file.
// If WATCHDOG_USEC is not set, the loop exits immediately (no watchdog configured).
func watchdogLoop(ctx context.Context, logger *slog.Logger) {
	usecStr := os.Getenv("WATCHDOG_USEC")
	if usecStr == "" {
		return
	}

	usec, err := strconv.ParseInt(usecStr, 10, 64)
	if err != nil || usec <= 0 {
		logger.Warn("invalid WATCHDOG_USEC, watchdog disabled", "value", usecStr)
		return
	}

	// Ping at half the watchdog interval (standard practice).
	interval := time.Duration(usec) * time.Microsecond / 2
	logger.Info("watchdog enabled", "interval", interval)

	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			sdNotify("WATCHDOG=1")
		}
	}
}

// sdNotify sends a message to systemd via the NOTIFY_SOCKET.
// Does nothing if NOTIFY_SOCKET is not set (i.e., not running under systemd).
func sdNotify(state string) {
	sock := os.Getenv("NOTIFY_SOCKET")
	if sock == "" {
		return
	}

	addr := &syscall.SockaddrUnix{Name: sock}
	fd, err := syscall.Socket(syscall.AF_UNIX, syscall.SOCK_DGRAM, 0)
	if err != nil {
		return
	}
	defer syscall.Close(fd)
	_ = syscall.Sendto(fd, []byte(state), 0, addr)
}
