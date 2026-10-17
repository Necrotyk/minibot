// SPDX-License-Identifier: AGPL-3.0-or-later
package irc

import (
	"context"
	"fmt"
	"log"
	"strings"
	"sync"

	"github.com/Necrotyk/minibot/internal/ipc"
)

// ServerConfig defines parameters for an IRC network connection.
type ServerConfig struct {
	Host             string   `json:"host"`
	Port             int      `json:"port"`
	Nick             string   `json:"nick"`
	Channels         []string `json:"channels"`
	SSL              bool     `json:"ssl"`
	Trusted          bool     `json:"trusted,omitempty"`
	TLSVerify        *bool    `json:"tls_verify,omitempty"`
	NickServPassword string   `json:"nickserv_password,omitempty"`
}

// Manager orchestrates multiple IRC connections across networks.
type Manager struct {
	ctx          context.Context
	cancel       context.CancelFunc
	serversMu    sync.RWMutex
	servers      map[string]ServerConfig
	connsMu      sync.RWMutex
	connections  map[string]*Connection
	broadcaster  func(ipc.OutboundBroadcast)
}

// NewManager initializes the IRC connection manager.
func NewManager(servers map[string]ServerConfig, broadcaster func(ipc.OutboundBroadcast)) *Manager {
	return &Manager{
		servers:     servers,
		connections: make(map[string]*Connection),
		broadcaster: broadcaster,
	}
}

// Start initiates connection loops for all configured initial servers.
func (m *Manager) Start(ctx context.Context) {
	m.ctx, m.cancel = context.WithCancel(ctx)

	m.serversMu.RLock()
	defer m.serversMu.RUnlock()

	for netName, srv := range m.servers {
		nick := srv.Nick
		if nick == "" {
			nick = "minibots"
		}
		channels := srv.Channels
		if len(channels) == 0 {
			channels = []string{"#minibots"}
		}
		port := srv.Port
		if port == 0 {
			port = 6667
		}

		m.spawnConnectionLocked(netName, nick, channels, srv)
	}
}

func (m *Manager) spawnConnectionLocked(network, nick string, channels []string, srv ServerConfig) *Connection {
	key := fmt.Sprintf("%s:%s", network, strings.ToLower(nick))

	m.connsMu.Lock()
	if existing, exists := m.connections[key]; exists && existing.isRunning() {
		m.connsMu.Unlock()
		return existing
	}
	m.connsMu.Unlock()

	var conn *Connection
	conn = NewConnection(
		network,
		srv.Host,
		srv.Port,
		nick,
		channels,
		srv.SSL,
		srv.NickServPassword,
		nil,
		func(net, n, line string) {
			if m.broadcaster != nil {
				m.broadcaster(ipc.OutboundBroadcast{
					Type:    "irc_line",
					Network: net,
					BotNick: n,
					Line:    line,
				})
			}
		},
		func(net, oldN, newN string) {
			m.connsMu.Lock()
			m.connections[fmt.Sprintf("%s:%s", net, strings.ToLower(newN))] = conn
			m.connsMu.Unlock()

			if m.broadcaster != nil {
				m.broadcaster(ipc.OutboundBroadcast{
					Type:    "nick_update",
					Network: net,
					OldNick: oldN,
					NewNick: newN,
				})
			}
		},
	)

	m.connsMu.Lock()
	m.connections[key] = conn
	m.connections[network] = conn // Default fallback
	m.connsMu.Unlock()

	log.Printf("[IRC] Spawning discrete socket connection for %s in %v", key, channels)
	conn.Start(m.ctx)
	return conn
}

// HandleCommand implements ipc.CommandHandler.
func (m *Manager) HandleCommand(cmd ipc.InboundCommand) error {
	network := cmd.Network
	if network == "" {
		network = "wireguard"
	}

	switch cmd.Type {
	case "raw":
		targetNick := cmd.Nick
		key := fmt.Sprintf("%s:%s", network, strings.ToLower(targetNick))

		m.connsMu.RLock()
		conn := m.connections[key]
		if conn == nil {
			conn = m.connections[network]
		}
		m.connsMu.RUnlock()

		if conn != nil {
			conn.SendRaw(cmd.Line)
			return nil
		}
		return fmt.Errorf("no active connection found for %s (%s)", key, network)

	case "spawn_conn":
		if cmd.Nick == "" {
			return fmt.Errorf("nick required for spawn_conn")
		}
		channels := cmd.Channels
		if len(channels) == 0 {
			channels = []string{"#minibots"}
		}

		m.serversMu.RLock()
		srv, exists := m.servers[network]
		m.serversMu.RUnlock()

		if !exists {
			srv = ServerConfig{
				Host: "127.0.0.1",
				Port: 6667,
				SSL:  false,
			}
		}
		if cmd.Host != "" {
			srv.Host = cmd.Host
		}
		if cmd.Port != 0 {
			srv.Port = cmd.Port
		}
		if cmd.SSL {
			srv.SSL = cmd.SSL
		}

		m.spawnConnectionLocked(network, cmd.Nick, channels, srv)
		return nil

	case "kill_conn":
		if cmd.Nick == "" {
			return fmt.Errorf("nick required for kill_conn")
		}
		key := fmt.Sprintf("%s:%s", network, strings.ToLower(cmd.Nick))

		m.connsMu.Lock()
		conn := m.connections[key]
		delete(m.connections, key)
		m.connsMu.Unlock()

		if conn != nil {
			creator := cmd.Creator
			if creator == "" {
				creator = "manager"
			}
			log.Printf("[IRC] Terminating discrete socket connection for %s by %s", key, creator)
			conn.Close(fmt.Sprintf("Minibot terminated by %s", creator))
			return nil
		}
		return fmt.Errorf("connection %s not found to kill", key)

	case "add_server":
		if cmd.Network == "" || cmd.Host == "" {
			return fmt.Errorf("network and host required for add_server")
		}
		port := cmd.Port
		if port == 0 {
			port = 6667
		}

		m.serversMu.Lock()
		m.servers[cmd.Network] = ServerConfig{
			Host: cmd.Host,
			Port: port,
			SSL:  cmd.SSL,
		}
		m.serversMu.Unlock()

		log.Printf("[IRC] Dynamically registered IRC network '%s': %s:%d (ssl=%v)", cmd.Network, cmd.Host, port, cmd.SSL)
		return nil

	default:
		return fmt.Errorf("unknown IPC command type: %s", cmd.Type)
	}
}

// Stop shuts down all active IRC connections cleanly.
func (m *Manager) Stop(quitMessage string) {
	if m.cancel != nil {
		m.cancel()
	}

	m.connsMu.Lock()
	seen := make(map[*Connection]bool)
	var conns []*Connection
	for _, conn := range m.connections {
		if !seen[conn] {
			seen[conn] = true
			conns = append(conns, conn)
		}
	}
	m.connections = make(map[string]*Connection)
	m.connsMu.Unlock()

	for _, conn := range conns {
		conn.Close(quitMessage)
	}
}
