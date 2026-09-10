// SPDX-License-Identifier: AGPL-3.0-or-later
package ipc

import (
	"bufio"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Server coordinates UNIX domain socket IPC connections and broadcasts.
type Server struct {
	socketPath   string
	allowedUsers []string
	handler      CommandHandler
	listener     net.Listener
	clientsMu    sync.RWMutex
	clients      map[net.Conn]bool
	stopChan     chan struct{}
	wg           sync.WaitGroup
}

// NewServer creates a new IPC server.
func NewServer(socketPath string, allowedUsers []string, handler CommandHandler) *Server {
	if len(allowedUsers) == 0 {
		allowedUsers = DefaultAuthorizedUsers
	}
	return &Server{
		socketPath:   socketPath,
		allowedUsers: allowedUsers,
		handler:      handler,
		clients:      make(map[net.Conn]bool),
		stopChan:     make(chan struct{}),
	}
}

// Start binds the UNIX domain socket and begins accepting client connections.
func (s *Server) Start() error {
	_ = os.Remove(s.socketPath)
	if err := os.MkdirAll(filepath.Dir(s.socketPath), 0750); err != nil {
		return fmt.Errorf("failed creating socket directory: %w", err)
	}

	addr, err := net.ResolveUnixAddr("unix", s.socketPath)
	if err != nil {
		return fmt.Errorf("failed resolving socket address: %w", err)
	}

	listener, err := net.ListenUnix("unix", addr)
	if err != nil {
		return fmt.Errorf("failed listening on unix socket: %w", err)
	}
	s.listener = listener

	// Enforce 0660 socket permissions
	if err := os.Chmod(s.socketPath, 0660); err != nil {
		_ = listener.Close()
		return fmt.Errorf("failed setting 0660 permissions on socket: %w", err)
	}

	log.Printf("[IPC] Minibots IPC server listening on %s (mode 0660)", s.socketPath)

	s.wg.Add(1)
	go s.acceptLoop(listener)
	return nil
}

func (s *Server) acceptLoop(listener *net.UnixListener) {
	defer s.wg.Done()
	for {
		conn, err := listener.AcceptUnix()
		if err != nil {
			select {
			case <-s.stopChan:
				return
			default:
				log.Printf("[IPC] Accept error: %v", err)
				continue
			}
		}

		// Enforce SO_PEERCRED authentication
		if err := VerifyPeerCredentialsWithAllowedUsers(conn, s.allowedUsers); err != nil {
			log.Printf("[IPC] Security verification failed: %v", err)
			_ = conn.Close()
			continue
		}

		s.clientsMu.Lock()
		s.clients[conn] = true
		s.clientsMu.Unlock()
		log.Printf("[IPC] Logic client connected from authenticated peer")

		s.wg.Add(1)
		go s.handleClient(conn)
	}
}

func (s *Server) handleClient(conn net.Conn) {
	defer s.wg.Done()
	defer func() {
		s.clientsMu.Lock()
		delete(s.clients, conn)
		s.clientsMu.Unlock()
		_ = conn.Close()
		log.Printf("[IPC] Logic client disconnected")
	}()

	scanner := bufio.NewScanner(conn)
	// Support up to 64KB lines
	buf := make([]byte, 64*1024)
	scanner.Buffer(buf, 64*1024)

	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}

		var cmd InboundCommand
		if err := json.Unmarshal(line, &cmd); err != nil {
			log.Printf("[IPC] Error decoding command: %v (raw: %s)", err, string(line))
			continue
		}

		if s.handler != nil {
			if err := s.handler.HandleCommand(cmd); err != nil {
				log.Printf("[IPC] Handler error for %s: %v", cmd.Type, err)
			}
		}
	}
}

// Broadcast sends an OutboundBroadcast payload to all connected logic clients.
func (s *Server) Broadcast(b OutboundBroadcast) {
	data, err := json.Marshal(b)
	if err != nil {
		log.Printf("[IPC] Error encoding broadcast: %v", err)
		return
	}
	data = append(data, '\n')

	s.clientsMu.RLock()
	defer s.clientsMu.RUnlock()

	for client := range s.clients {
		go func(c net.Conn) {
			_ = c.SetWriteDeadline(time.Now().Add(5 * time.Second))
			_, _ = c.Write(data)
		}(client)
	}
}

// Close shuts down the server and cleans up the UNIX socket.
func (s *Server) Close() error {
	close(s.stopChan)
	if s.listener != nil {
		_ = s.listener.Close()
	}

	s.clientsMu.Lock()
	for client := range s.clients {
		_ = client.Close()
	}
	s.clients = make(map[net.Conn]bool)
	s.clientsMu.Unlock()

	s.wg.Wait()
	_ = os.Remove(s.socketPath)
	return nil
}
