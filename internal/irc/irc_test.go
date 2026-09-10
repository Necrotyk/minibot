// SPDX-License-Identifier: AGPL-3.0-or-later
package irc

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Necrotyk/minibot/internal/ipc"
)

func TestTokenBucket(t *testing.T) {
	tb := NewTokenBucket(2.0, 10.0) // 2 tokens capacity, refills 10 tokens/sec

	// Should take 2 tokens immediately
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	if !tb.TakeContext(ctx) {
		t.Errorf("expected 1st token immediately")
	}
	if !tb.TakeContext(ctx) {
		t.Errorf("expected 2nd token immediately")
	}

	// Bucket is empty, test cancellation timeout
	failCtx, failCancel := context.WithTimeout(context.Background(), 1*time.Millisecond)
	defer failCancel()

	// Wait 1ms so context expires
	time.Sleep(2 * time.Millisecond)
	if tb.TakeContext(failCtx) {
		t.Errorf("expected TakeContext to return false on expired context")
	}
}

func TestConnectionInboundHandling(t *testing.T) {
	var linesReceived []string
	var nickUpdates []string
	var mu sync.Mutex

	conn := NewConnection(
		"testnet",
		"127.0.0.1",
		6667,
		"minibot",
		[]string{"#dev", "#ops"},
		false,
		"secretpass",
		nil,
		func(net, n, line string) {
			mu.Lock()
			linesReceived = append(linesReceived, line)
			mu.Unlock()
		},
		func(net, oldN, newN string) {
			mu.Lock()
			nickUpdates = append(nickUpdates, oldN+"->"+newN)
			mu.Unlock()
		},
	)

	// 1. Test PING handling
	conn.handleInboundLine("PING :server.local")
	select {
	case line := <-conn.sendQueue:
		if line != "PONG :server.local" {
			t.Errorf("expected 'PONG :server.local', got %q", line)
		}
	default:
		t.Errorf("expected PONG response in sendQueue")
	}

	// 2. Test 433 Nick collision handling
	conn.handleInboundLine(":server 433 * minibot :Nickname is already in use.")
	select {
	case line := <-conn.sendQueue:
		if line != "NICK minibot_" {
			t.Errorf("expected 'NICK minibot_', got %q", line)
		}
	default:
		t.Errorf("expected NICK rename in sendQueue")
	}

	mu.Lock()
	if len(nickUpdates) != 1 || nickUpdates[0] != "minibot->minibot_" {
		t.Errorf("unexpected nickUpdates: %v", nickUpdates)
	}
	mu.Unlock()

	// 3. Test 001 RPL_WELCOME registration
	conn.handleInboundLine(":server 001 minibot_ :Welcome to the IRC network")
	// Should queue NickServ IDENTIFY and JOINs
	var queued []string
drainLoop:
	for {
		select {
		case line := <-conn.sendQueue:
			queued = append(queued, line)
		default:
			break drainLoop
		}
	}

	expectedJoined := []string{
		"PRIVMSG NickServ :IDENTIFY secretpass",
		"JOIN #dev",
		"JOIN #ops",
	}

	if len(queued) < len(expectedJoined) {
		t.Fatalf("expected at least %d queued messages on 001, got %d: %v", len(expectedJoined), len(queued), queued)
	}

	for i, exp := range expectedJoined {
		if queued[i] != exp {
			t.Errorf("expected queued[%d] = %q, got %q", i, exp, queued[i])
		}
	}

	mu.Lock()
	if len(linesReceived) != 2 {
		t.Errorf("expected 2 linesReceived (PING handled internally), got %d", len(linesReceived))
	}
	mu.Unlock()
}

func TestManagerLifecycleAndCommands(t *testing.T) {
	var broadcasts []ipc.OutboundBroadcast
	var mu sync.Mutex

	servers := map[string]ServerConfig{
		"wireguard": {
			Host:     "127.0.0.1",
			Port:     6667,
			Nick:     "masterbot",
			Channels: []string{"#lobby"},
			SSL:      false,
		},
	}

	mgr := NewManager(servers, func(b ipc.OutboundBroadcast) {
		mu.Lock()
		broadcasts = append(broadcasts, b)
		mu.Unlock()
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	mgr.Start(ctx)
	defer mgr.Stop("Test finished")

	// 1. Test add_server
	err := mgr.HandleCommand(ipc.InboundCommand{
		Type:    "add_server",
		Network: "freenode",
		Host:    "irc.freenode.net",
		Port:    6667,
		SSL:     false,
	})
	if err != nil {
		t.Fatalf("add_server failed: %v", err)
	}

	mgr.serversMu.RLock()
	fnServer, exists := mgr.servers["freenode"]
	mgr.serversMu.RUnlock()
	if !exists || fnServer.Host != "irc.freenode.net" {
		t.Errorf("freenode server not properly registered: %+v", fnServer)
	}

	// 2. Test spawn_conn
	err = mgr.HandleCommand(ipc.InboundCommand{
		Type:     "spawn_conn",
		Network:  "freenode",
		Nick:     "worker1",
		Channels: []string{"#worker-pool"},
	})
	if err != nil {
		t.Fatalf("spawn_conn failed: %v", err)
	}

	mgr.connsMu.RLock()
	workerConn := mgr.connections["freenode:worker1"]
	mgr.connsMu.RUnlock()
	if workerConn == nil {
		t.Fatalf("expected freenode:worker1 connection to be spawned")
	}

	// 3. Test raw command delivery
	err = mgr.HandleCommand(ipc.InboundCommand{
		Type:    "raw",
		Network: "freenode",
		Nick:    "worker1",
		Line:    "PRIVMSG #worker-pool :task accepted",
	})
	if err != nil {
		t.Fatalf("raw command failed: %v", err)
	}

	select {
	case line := <-workerConn.sendQueue:
		if line != "PRIVMSG #worker-pool :task accepted" {
			t.Errorf("unexpected raw line in queue: %s", line)
		}
	case <-time.After(500 * time.Millisecond):
		t.Errorf("timed out waiting for raw line in sendQueue")
	}

	// 4. Test kill_conn
	err = mgr.HandleCommand(ipc.InboundCommand{
		Type:    "kill_conn",
		Network: "freenode",
		Nick:    "worker1",
		Creator: "admin",
	})
	if err != nil {
		t.Fatalf("kill_conn failed: %v", err)
	}

	mgr.connsMu.RLock()
	deletedConn := mgr.connections["freenode:worker1"]
	mgr.connsMu.RUnlock()
	if deletedConn != nil {
		t.Errorf("expected freenode:worker1 connection to be removed from manager map")
	}

	// 5. Test unknown command
	err = mgr.HandleCommand(ipc.InboundCommand{
		Type: "unknown_command_type",
	})
	if err == nil || !strings.Contains(err.Error(), "unknown IPC command") {
		t.Errorf("expected error for unknown command type, got: %v", err)
	}
}
