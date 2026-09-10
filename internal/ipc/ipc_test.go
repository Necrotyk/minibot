// SPDX-License-Identifier: AGPL-3.0-or-later
package ipc

import (
	"bufio"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

type mockHandler struct {
	mu       sync.Mutex
	commands []InboundCommand
}

func (m *mockHandler) HandleCommand(cmd InboundCommand) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.commands = append(m.commands, cmd)
	return nil
}

func (m *mockHandler) getCommands() []InboundCommand {
	m.mu.Lock()
	defer m.mu.Unlock()
	res := make([]InboundCommand, len(m.commands))
	copy(res, m.commands)
	return res
}

func TestIPCServerLifecycleAndFraming(t *testing.T) {
	tmpDir, err := os.MkdirTemp("", "minibots_ipc_test_*")
	if err != nil {
		t.Fatalf("failed creating temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	sockPath := filepath.Join(tmpDir, "test_ipc.sock")
	handler := &mockHandler{}
	server := NewServer(sockPath, DefaultAuthorizedUsers, handler)

	if err := server.Start(); err != nil {
		t.Fatalf("failed to start IPC server: %v", err)
	}
	defer server.Close()

	// Verify socket permissions (0660)
	fi, err := os.Stat(sockPath)
	if err != nil {
		t.Fatalf("failed to stat socket: %v", err)
	}
	mode := fi.Mode().Perm()
	if mode != 0660 {
		t.Errorf("expected socket permissions 0660, got %04o", mode)
	}

	// Connect client
	clientConn, err := net.Dial("unix", sockPath)
	if err != nil {
		t.Fatalf("failed to dial unix socket: %v", err)
	}
	defer clientConn.Close()

	// Wait briefly for accept loop
	time.Sleep(50 * time.Millisecond)

	// Send an InboundCommand
	cmd := InboundCommand{
		Type:    "raw",
		Network: "wireguard",
		Nick:    "testbot",
		Line:    "PRIVMSG #test :hello world",
	}
	cmdBytes, err := json.Marshal(cmd)
	if err != nil {
		t.Fatalf("failed to marshal command: %v", err)
	}
	cmdBytes = append(cmdBytes, '\n')
	if _, err := clientConn.Write(cmdBytes); err != nil {
		t.Fatalf("failed writing command to socket: %v", err)
	}

	// Verify command received by handler
	deadline := time.Now().Add(2 * time.Second)
	var cmds []InboundCommand
	for time.Now().Before(deadline) {
		cmds = handler.getCommands()
		if len(cmds) > 0 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}

	if len(cmds) != 1 {
		t.Fatalf("expected 1 command handled, got %d", len(cmds))
	}
	if cmds[0].Type != "raw" || cmds[0].Nick != "testbot" || cmds[0].Line != "PRIVMSG #test :hello world" {
		t.Errorf("unexpected command payload: %+v", cmds[0])
	}

	// Test Broadcast from server to client
	broadcastMsg := OutboundBroadcast{
		Type:    "irc_line",
		Network: "wireguard",
		BotNick: "testbot",
		Line:    ":nick!user@host PRIVMSG #test :response",
	}
	server.Broadcast(broadcastMsg)

	// Read on client side
	_ = clientConn.SetReadDeadline(time.Now().Add(2 * time.Second))
	reader := bufio.NewReader(clientConn)
	line, err := reader.ReadBytes('\n')
	if err != nil {
		t.Fatalf("failed reading broadcast line: %v", err)
	}

	var received OutboundBroadcast
	if err := json.Unmarshal(line, &received); err != nil {
		t.Fatalf("failed unmarshaling broadcast: %v", err)
	}
	if received.Type != "irc_line" || received.BotNick != "testbot" || received.Line != broadcastMsg.Line {
		t.Errorf("unexpected broadcast payload: %+v", received)
	}
}

func TestVerifyPeerCredentials(t *testing.T) {
	tmpDir, err := os.MkdirTemp("", "minibots_ucred_test_*")
	if err != nil {
		t.Fatalf("failed creating temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	sockPath := filepath.Join(tmpDir, "ucred.sock")
	addr, err := net.ResolveUnixAddr("unix", sockPath)
	if err != nil {
		t.Fatalf("failed resolving unix addr: %v", err)
	}

	listener, err := net.ListenUnix("unix", addr)
	if err != nil {
		t.Fatalf("failed listening: %v", err)
	}
	defer listener.Close()

	errChan := make(chan error, 1)
	go func() {
		conn, err := listener.AcceptUnix()
		if err != nil {
			errChan <- err
			return
		}
		defer conn.Close()
		errChan <- VerifyPeerCredentials(conn)
	}()

	client, err := net.DialUnix("unix", nil, addr)
	if err != nil {
		t.Fatalf("failed to dial unix: %v", err)
	}
	defer client.Close()

	select {
	case err := <-errChan:
		if err != nil {
			t.Errorf("expected current process UID to be authorized, got error: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("peer credential verification timed out")
	}
}
