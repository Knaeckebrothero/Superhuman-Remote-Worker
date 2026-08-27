package config

import (
	"fmt"
	"strings"
	"testing"
)

func TestTransportPrefersHTTP(t *testing.T) {
	cfg := Defaults()
	cfg.Orchestrator.URL = "http://orchestrator:8085"
	cfg.Orchestrator.Token = strings.Repeat("a", 64)
	cfg.NATS.URL = "nats://nats:4222"

	got, err := cfg.Transport()
	if err != nil || got != "http" {
		t.Fatalf("Transport() = %q, %v", got, err)
	}
}

func TestTransportRefusesUnauthenticatedNATSForPartialHTTPConfig(t *testing.T) {
	cfg := Defaults()
	cfg.Orchestrator.URL = "http://orchestrator:8085"
	cfg.NATS.URL = "nats://nats:4222"

	got, err := cfg.Transport()
	if err == nil || got != "" {
		t.Fatalf("Transport() = %q, %v", got, err)
	}
}

func TestTransportFailsWithoutConfiguration(t *testing.T) {
	cfg := Defaults()
	got, err := cfg.Transport()
	if err == nil || got != "" {
		t.Fatalf("Transport() = %q, %v", got, err)
	}
}

func TestLoadEnvironmentOverrides(t *testing.T) {
	t.Setenv("ORCHESTRATOR_URL", "http://orchestrator:8085")
	t.Setenv("VM_AUTH_TOKEN", strings.Repeat("b", 64))
	t.Setenv("ENTITY_ID", "entity-1")
	t.Setenv("JOB_ID", "entity-1")
	t.Setenv("VM_ID", "agent-vm-entity-1")
	t.Setenv("NATS_URL", "")

	cfg, err := Load("")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Orchestrator.URL != "http://orchestrator:8085" || cfg.EntityID != "entity-1" {
		t.Fatal("ORCHESTRATOR_URL or ENTITY_ID did not override config")
	}
	if cfg.Orchestrator.Token != strings.Repeat("b", 64) {
		t.Fatal("VM_AUTH_TOKEN did not override config")
	}
}

func TestOrchestratorConfigStringRedactsToken(t *testing.T) {
	token := strings.Repeat("c", 64)
	cfg := &Config{Orchestrator: OrchestratorConfig{
		URL:   "http://orchestrator:8085",
		Token: token,
	}}
	formatted := fmt.Sprintf("%+v", cfg)
	if strings.Contains(formatted, token) {
		t.Fatal("formatted config exposed the orchestrator token")
	}
	if !strings.Contains(formatted, "[REDACTED]") {
		t.Fatal("formatted config did not include a redaction marker")
	}
}
