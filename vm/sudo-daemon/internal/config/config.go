// Package config handles YAML configuration for the sudo-gated daemon.
package config

import (
	"fmt"
	"os"
	"time"

	"gopkg.in/yaml.v3"
)

// Config holds the daemon's runtime configuration.
type Config struct {
	// SocketPath is the Unix domain socket the daemon listens on.
	SocketPath string `yaml:"socket_path"`

	// NATS connection settings.
	NATS NATSConfig `yaml:"nats"`

	// RateLimit controls per-socket request throttling.
	RateLimit RateLimitConfig `yaml:"rate_limit"`

	// Timeouts for various operations.
	Timeouts TimeoutConfig `yaml:"timeouts"`

	// JobID identifies the job this VM serves. Set via env var or cloud-init.
	JobID string `yaml:"job_id"`

	// VMID identifies this VM instance. Defaults to "agent-vm-{job_id}".
	VMID string `yaml:"vm_id"`

	// LogLevel controls daemon logging verbosity (debug, info, warn, error).
	LogLevel string `yaml:"log_level"`
}

// NATSConfig holds NATS connection parameters.
type NATSConfig struct {
	URL               string        `yaml:"url"`
	MaxReconnects     int           `yaml:"max_reconnects"`
	ReconnectWait     time.Duration `yaml:"reconnect_wait"`
	CredentialsFile   string        `yaml:"credentials_file"`
	SubjectPrefix     string        `yaml:"subject_prefix"`
}

// RateLimitConfig controls request throttling.
type RateLimitConfig struct {
	// RequestsPerMinute is the sustained rate.
	RequestsPerMinute float64 `yaml:"requests_per_minute"`
	// Burst is the maximum burst size.
	Burst int `yaml:"burst"`
}

// TimeoutConfig holds timeout durations.
type TimeoutConfig struct {
	// NATSRequest is how long the daemon waits for an orchestrator reply.
	NATSRequest time.Duration `yaml:"nats_request"`
	// SocketRead is how long to wait for the plugin to send a complete request.
	SocketRead time.Duration `yaml:"socket_read"`
	// GracefulShutdown is how long to wait for in-flight requests on shutdown.
	GracefulShutdown time.Duration `yaml:"graceful_shutdown"`
}

// Defaults returns a Config with sensible defaults.
func Defaults() *Config {
	return &Config{
		SocketPath: "/run/sudo-gated/sudo-gated.sock",
		NATS: NATSConfig{
			URL:           "nats://127.0.0.1:4222",
			MaxReconnects: -1,
			ReconnectWait: 2 * time.Second,
			SubjectPrefix: "sudo.request",
		},
		RateLimit: RateLimitConfig{
			RequestsPerMinute: 5,
			Burst:             3,
		},
		Timeouts: TimeoutConfig{
			NATSRequest:      300 * time.Second,
			SocketRead:       10 * time.Second,
			GracefulShutdown: 30 * time.Second,
		},
		LogLevel: "info",
	}
}

// Load reads a YAML config file and merges it with defaults.
// Environment variables override file values:
//
//	NATS_URL  → Config.NATS.URL
//	JOB_ID    → Config.JobID
//	VM_ID     → Config.VMID
func Load(path string) (*Config, error) {
	cfg := Defaults()

	if path != "" {
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read config %s: %w", path, err)
		}
		if err := yaml.Unmarshal(data, cfg); err != nil {
			return nil, fmt.Errorf("parse config %s: %w", path, err)
		}
	}

	// Environment overrides.
	if v := os.Getenv("NATS_URL"); v != "" {
		cfg.NATS.URL = v
	}
	if v := os.Getenv("JOB_ID"); v != "" {
		cfg.JobID = v
	}
	if v := os.Getenv("VM_ID"); v != "" {
		cfg.VMID = v
	}

	// Derive VM_ID from JOB_ID if not set.
	if cfg.VMID == "" && cfg.JobID != "" {
		cfg.VMID = "agent-vm-" + cfg.JobID
	}

	return cfg, nil
}
