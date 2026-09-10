// SPDX-License-Identifier: AGPL-3.0-or-later
package ipc

// InboundCommand represents a JSON-RPC directive from logic daemons.
type InboundCommand struct {
	Type     string   `json:"type"`              // "raw", "spawn_conn", "kill_conn", "add_server"
	Network  string   `json:"network,omitempty"`  // Target IRC network name
	Nick     string   `json:"nick,omitempty"`     // Target bot nick
	Line     string   `json:"line,omitempty"`     // Raw IRC line to transmit
	Channels []string `json:"channels,omitempty"` // Channels to join on spawn
	Host     string   `json:"host,omitempty"`     // IRC host
	Port     int      `json:"port,omitempty"`     // IRC port
	SSL      bool     `json:"ssl,omitempty"`      // Use TLS/SSL
	Creator  string   `json:"creator,omitempty"`  // Initiator of kill command
}

// OutboundBroadcast represents an event notification sent to logic daemons.
type OutboundBroadcast struct {
	Type     string `json:"type"`               // "irc_line", "nick_update"
	Network  string `json:"network"`            // IRC network name
	BotNick  string `json:"bot_nick,omitempty"` // Current bot nickname
	Line     string `json:"line,omitempty"`     // Full raw IRC line received
	OldNick  string `json:"old_nick,omitempty"` // Previous nickname
	NewNick  string `json:"new_nick,omitempty"` // New nickname
}

// CommandHandler processes incoming IPC commands.
type CommandHandler interface {
	HandleCommand(cmd InboundCommand) error
}
