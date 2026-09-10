// SPDX-License-Identifier: AGPL-3.0-or-later
package config

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/Necrotyk/minibot/internal/irc"
)

// Config encapsulates runtime parameters for Minibots connection and logic components.
type Config struct {
	InternalServer string                      `json:"INTERNAL_SERVER"`
	AdminNicks     []string                    `json:"ADMIN_NICKS"`
	Servers        map[string]irc.ServerConfig `json:"SERVERS"`
}

var envVarRegex = regexp.MustCompile(`\$\{([^}]+)\}`)

// ResolveSecret searches systemd LoadCredential, OS environment, and zero-trust .secrets.json files.
func ResolveSecret(key string) string {
	// 1. systemd LoadCredential directory
	if credDir := os.Getenv("CREDENTIALS_DIRECTORY"); credDir != "" {
		credPath := filepath.Join(credDir, key)
		if data, err := os.ReadFile(credPath); err == nil {
			return strings.TrimSpace(string(data))
		}
	}

	// 2. OS Environment variable
	if val := os.Getenv(key); val != "" {
		return val
	}

	// 3. Local and user keystore files
	homeDir, _ := os.UserHomeDir()
	candidateFiles := []string{
		filepath.Join(homeDir, ".secrets.json"),
		filepath.Join(homeDir, ".config", "minibots", "secrets.json"),
		".secrets.json",
	}
	for _, f := range candidateFiles {
		if data, err := os.ReadFile(f); err == nil {
			var secMap map[string]string
			if err := json.Unmarshal(data, &secMap); err == nil {
				if val, ok := secMap[key]; ok && val != "" {
					return val
				}
			}
		}
	}

	return ""
}

// ExpandEnvVars replaces ${VAR} or ${VAR:-default} patterns using ResolveSecret.
func ExpandEnvVars(raw string) string {
	return envVarRegex.ReplaceAllStringFunc(raw, func(m string) string {
		match := envVarRegex.FindStringSubmatch(m)
		if len(match) < 2 {
			return m
		}
		expr := match[1]
		parts := strings.SplitN(expr, ":-", 2)
		varName := parts[0]
		defaultVal := ""
		if len(parts) == 2 {
			defaultVal = parts[1]
		}

		val := ResolveSecret(varName)
		if val != "" {
			return val
		}
		return defaultVal
	})
}

// LoadConfig reads, expands, and validates configuration from the specified or discovered path.
func LoadConfig(customPath string) (*Config, error) {
	var targetPath string

	if customPath != "" {
		targetPath = customPath
	} else if envPath := os.Getenv("MINIBOTS_CONFIG"); envPath != "" {
		targetPath = envPath
	} else {
		candidates := []string{
			"/opt/minibots/config.json",
			"config.json",
			"config.example.json",
		}
		for _, c := range candidates {
			if _, err := os.Stat(c); err == nil {
				targetPath = c
				break
			}
		}
	}

	cfg := &Config{
		InternalServer: "127.0.0.1",
		AdminNicks:     []string{"admin*"},
		Servers:        make(map[string]irc.ServerConfig),
	}

	if targetPath == "" {
		applyDefaults(cfg)
		return cfg, fmt.Errorf("no config file found, applied default fallback")
	}

	content, err := os.ReadFile(targetPath)
	if err != nil {
		applyDefaults(cfg)
		return cfg, fmt.Errorf("failed reading config from %s: %w", targetPath, err)
	}

	expanded := ExpandEnvVars(string(content))

	// Flexible unmarshaler to support both uppercase and lowercase keys
	var rawMap map[string]json.RawMessage
	if err := json.Unmarshal([]byte(expanded), &rawMap); err != nil {
		applyDefaults(cfg)
		return cfg, fmt.Errorf("failed unmarshaling json from %s: %w", targetPath, err)
	}

	if val, ok := rawMap["INTERNAL_SERVER"]; ok {
		_ = json.Unmarshal(val, &cfg.InternalServer)
	} else if val, ok := rawMap["internal_server"]; ok {
		_ = json.Unmarshal(val, &cfg.InternalServer)
	}

	if val, ok := rawMap["ADMIN_NICKS"]; ok {
		_ = json.Unmarshal(val, &cfg.AdminNicks)
	} else if val, ok := rawMap["admin_nicks"]; ok {
		_ = json.Unmarshal(val, &cfg.AdminNicks)
	}

	if val, ok := rawMap["SERVERS"]; ok {
		_ = json.Unmarshal(val, &cfg.Servers)
	} else if val, ok := rawMap["servers"]; ok {
		_ = json.Unmarshal(val, &cfg.Servers)
	}

	applyDefaults(cfg)
	log.Printf("[Config] Loaded configuration from %s with %d server network(s)", targetPath, len(cfg.Servers))
	return cfg, nil
}

func applyDefaults(cfg *Config) {
	if cfg.Servers == nil {
		cfg.Servers = make(map[string]irc.ServerConfig)
	}

	if len(cfg.Servers) == 0 {
		cfg.Servers["local"] = irc.ServerConfig{
			Host:     "127.0.0.1",
			Port:     6667,
			Nick:     "minibot",
			Channels: []string{"#minibot", "#dev"},
			SSL:      false,
		}
	} else {
		for name, srv := range cfg.Servers {
			if srv.Nick == "" {
				srv.Nick = "minibots"
			}
			if len(srv.Channels) == 0 {
				srv.Channels = []string{"#minibots"}
			}
			if srv.Port == 0 {
				if srv.SSL {
					srv.Port = 6697
				} else {
					srv.Port = 6667
				}
			}
			cfg.Servers[name] = srv
		}
	}
}
