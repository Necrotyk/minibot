// SPDX-License-Identifier: AGPL-3.0-or-later
package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestExpandEnvVars(t *testing.T) {
	t.Setenv("TEST_MINIBOTS_TOKEN", "secret123")
	t.Setenv("EMPTY_VAR", "")

	tests := []struct {
		input    string
		expected string
	}{
		{"prefix_${TEST_MINIBOTS_TOKEN}_suffix", "prefix_secret123_suffix"},
		{"${TEST_MINIBOTS_TOKEN}", "secret123"},
		{"${NONEXISTENT_VAR:-fallback_value}", "fallback_value"},
		{"no_vars_here", "no_vars_here"},
	}

	for _, tc := range tests {
		got := ExpandEnvVars(tc.input)
		if got != tc.expected {
			t.Errorf("ExpandEnvVars(%q) = %q; expected %q", tc.input, got, tc.expected)
		}
	}
}

func TestLoadConfigCustomJSON(t *testing.T) {
	tmpDir, err := os.MkdirTemp("", "minibots_cfg_test_*")
	if err != nil {
		t.Fatalf("failed creating temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	t.Setenv("TEST_NICKSERV_PASS", "mypassword99")

	cfgJSON := `{
		"INTERNAL_SERVER": "127.0.0.5",
		"ADMIN_NICKS": ["operator*", "root*"],
		"SERVERS": {
			"customnet": {
				"host": "irc.custom.org",
				"port": 6697,
				"nick": "mybot",
				"channels": ["#channel1", "#channel2"],
				"ssl": true,
				"nickserv_password": "${TEST_NICKSERV_PASS}"
			}
		}
	}`

	cfgPath := filepath.Join(tmpDir, "config.json")
	if err := os.WriteFile(cfgPath, []byte(cfgJSON), 0600); err != nil {
		t.Fatalf("failed writing test config: %v", err)
	}

	cfg, err := LoadConfig(cfgPath)
	if err != nil {
		t.Fatalf("failed to load config: %v", err)
	}

	if cfg.InternalServer != "127.0.0.5" {
		t.Errorf("expected InternalServer 127.0.0.5, got %s", cfg.InternalServer)
	}
	if len(cfg.AdminNicks) != 2 || cfg.AdminNicks[0] != "operator*" {
		t.Errorf("unexpected AdminNicks: %v", cfg.AdminNicks)
	}

	srv, exists := cfg.Servers["customnet"]
	if !exists {
		t.Fatalf("expected customnet in servers, got: %v", cfg.Servers)
	}
	if srv.Host != "irc.custom.org" || srv.Port != 6697 || srv.Nick != "mybot" || !srv.SSL {
		t.Errorf("unexpected customnet server config: %+v", srv)
	}
	if srv.NickServPassword != "mypassword99" {
		t.Errorf("expected expanded nickserv_password 'mypassword99', got %q", srv.NickServPassword)
	}
	if len(srv.Channels) != 2 || srv.Channels[0] != "#channel1" {
		t.Errorf("unexpected customnet channels: %v", srv.Channels)
	}
}

func TestLoadConfigDefaultFallback(t *testing.T) {
	cfg, err := LoadConfig("/nonexistent/path/to/config.json")
	if err == nil {
		t.Fatalf("expected error when config file does not exist")
	}
	if cfg == nil {
		t.Fatalf("expected non-nil config fallback")
	}

	srv, exists := cfg.Servers["local"]
	if !exists {
		t.Fatalf("expected default local network in fallback")
	}
	if srv.Host != "127.0.0.1" || srv.Port != 6667 || srv.Nick != "minibot" {
		t.Errorf("unexpected fallback local config: %+v", srv)
	}
}
