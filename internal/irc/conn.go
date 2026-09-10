// SPDX-License-Identifier: AGPL-3.0-or-later
package irc

import (
	"bufio"
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"log"
	"net"
	"strings"
	"sync"
	"time"
)

// LineCallback is called when an IRC line is received.
type LineCallback func(network, nick, line string)

// NickCallback is called when a nick collision causes a rename.
type NickCallback func(network, oldNick, newNick string)

// Connection manages an individual IRC TCP/TLS socket session.
type Connection struct {
	Network          string
	Host             string
	Port             int
	DesiredNick      string
	CurrentNick      string
	Channels         []string
	UseSSL           bool
	NickServPassword string
	RateLimiter      *TokenBucket

	onLineReceived LineCallback
	onNickUpdated  NickCallback

	mu           sync.Mutex
	conn         net.Conn
	reader       *bufio.Reader
	sendQueue    chan string
	isConnected  bool
	isRegistered bool
	running      bool
	cancelFunc   context.CancelFunc
}

// NewConnection initializes a discrete IRC connection instance.
func NewConnection(
	network, host string,
	port int,
	nick string,
	channels []string,
	useSSL bool,
	nickServPassword string,
	rateLimiter *TokenBucket,
	onLine LineCallback,
	onNick NickCallback,
) *Connection {
	if rateLimiter == nil {
		if strings.HasPrefix(network, "wireguard") {
			rateLimiter = NewTokenBucket(20.0, 10.0)
		} else {
			rateLimiter = NewTokenBucket(4.0, 0.5)
		}
	}

	return &Connection{
		Network:          network,
		Host:             host,
		Port:             port,
		DesiredNick:      nick,
		CurrentNick:      nick,
		Channels:         channels,
		UseSSL:           useSSL,
		NickServPassword: nickServPassword,
		RateLimiter:      rateLimiter,
		onLineReceived:   onLine,
		onNickUpdated:    onNick,
		sendQueue:        make(chan string, 1000),
		running:          true,
	}
}

// Start begins connection lifecycle and auto-reconnect loop.
func (c *Connection) Start(ctx context.Context) {
	connCtx, cancel := context.WithCancel(ctx)
	c.mu.Lock()
	c.cancelFunc = cancel
	c.mu.Unlock()

	go c.runLoop(connCtx)
}

func (c *Connection) runLoop(ctx context.Context) {
	backoff := 5 * time.Second

	for c.isRunning() {
		select {
		case <-ctx.Done():
			return
		default:
		}

		err := c.connectAndServe(ctx)
		if err != nil && c.isRunning() {
			log.Printf("[%s:%s] Disconnected: %v. Reconnecting in %v...", c.Network, c.CurrentNick, err, backoff)
			select {
			case <-ctx.Done():
				return
			case <-time.After(backoff):
				backoff *= 2
				if backoff > 60*time.Second {
					backoff = 60 * time.Second
				}
			}
		} else {
			backoff = 5 * time.Second
		}
	}
}

func (c *Connection) connectAndServe(ctx context.Context) error {
	addr := fmt.Sprintf("%s:%d", c.Host, c.Port)
	log.Printf("[%s:%s] Connecting to %s (SSL: %v)...", c.Network, c.CurrentNick, addr, c.UseSSL)

	dialer := &net.Dialer{Timeout: 15 * time.Second}
	var rawConn net.Conn
	var err error

	if c.UseSSL {
		tlsConfig := &tls.Config{
			ServerName: c.Host,
		}
		if !strings.HasPrefix(c.Network, "wireguard") {
			// Skip cert validation on public self-signed servers if needed
			tlsConfig.InsecureSkipVerify = true
		}
		rawConn, err = tls.DialWithDialer(dialer, "tcp", addr, tlsConfig)
	} else {
		rawConn, err = dialer.DialContext(ctx, "tcp", addr)
	}

	if err != nil {
		return fmt.Errorf("dial error: %w", err)
	}

	c.mu.Lock()
	c.conn = rawConn
	c.reader = bufio.NewReader(rawConn)
	c.isConnected = true
	c.isRegistered = false
	c.mu.Unlock()

	log.Printf("[%s:%s] TCP connected to %s", c.Network, c.CurrentNick, addr)

	// Send initial registration
	c.SendRaw(fmt.Sprintf("NICK %s", c.CurrentNick))
	c.SendRaw(fmt.Sprintf("USER %s 0 * :Dystopi Minibots Daemon", c.CurrentNick))

	innerCtx, innerCancel := context.WithCancel(ctx)
	defer innerCancel()

	var wg sync.WaitGroup
	errChan := make(chan error, 2)

	// Sender routine
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-innerCtx.Done():
				return
			case line, ok := <-c.sendQueue:
				if !ok {
					return
				}
				if !c.RateLimiter.TakeContext(innerCtx) {
					return
				}
				c.mu.Lock()
				conn := c.conn
				c.mu.Unlock()
				if conn == nil {
					return
				}
				_ = conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
				if _, err := fmt.Fprintf(conn, "%s\r\n", line); err != nil {
					select {
					case errChan <- fmt.Errorf("write error: %w", err):
					default:
					}
					return
				}
			}
		}
	}()

	// Receiver routine
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-innerCtx.Done():
				return
			default:
			}

			c.mu.Lock()
			r := c.reader
			conn := c.conn
			c.mu.Unlock()
			if r == nil || conn == nil {
				return
			}

			// Read up to 3 minutes without traffic before triggering timeout
			_ = conn.SetReadDeadline(time.Now().Add(180 * time.Second))
			line, err := r.ReadString('\n')
			if err != nil {
				if innerCtx.Err() == nil {
					if err != io.EOF {
						select {
						case errChan <- fmt.Errorf("read error: %w", err):
						default:
						}
					} else {
						select {
						case errChan <- fmt.Errorf("EOF from server"):
						default:
						}
					}
				}
				return
			}

			line = strings.TrimRight(line, "\r\n")
			if len(line) == 0 {
				continue
			}

			c.handleInboundLine(line)
		}
	}()

	select {
	case <-ctx.Done():
		innerCancel()
		_ = rawConn.Close()
		wg.Wait()
		return nil
	case err := <-errChan:
		innerCancel()
		_ = rawConn.Close()
		wg.Wait()
		return err
	}
}

func (c *Connection) handleInboundLine(line string) {
	// Respond immediately to PING
	if strings.HasPrefix(line, "PING ") {
		token := strings.TrimPrefix(line, "PING ")
		c.SendRaw(fmt.Sprintf("PONG %s", token))
		return
	}

	parts := strings.Fields(line)
	if len(parts) >= 2 {
		cmd := parts[1]
		if !strings.HasPrefix(parts[0], ":") {
			cmd = parts[0]
		}

		// RPL_WELCOME 001
		if cmd == "001" {
			c.mu.Lock()
			c.isRegistered = true
			if len(parts) > 2 {
				c.CurrentNick = parts[2]
			}
			c.mu.Unlock()
			log.Printf("[%s:%s] Registered successfully with server", c.Network, c.CurrentNick)

			// NickServ identification
			if c.NickServPassword != "" {
				c.SendRaw(fmt.Sprintf("PRIVMSG NickServ :IDENTIFY %s", c.NickServPassword))
			}

			// Auto-join configured channels
			for _, ch := range c.Channels {
				c.SendRaw(fmt.Sprintf("JOIN %s", ch))
			}
		} else if cmd == "433" {
			// Nick collision 433 ERR_NICKNAMEINUSE
			c.mu.Lock()
			registered := c.isRegistered
			c.mu.Unlock()

			if !registered {
				oldNick := c.CurrentNick
				var takenNick string
				if len(parts) > 3 {
					takenNick = parts[3]
				} else if len(parts) > 2 {
					takenNick = parts[2]
				} else {
					takenNick = c.CurrentNick
				}
				newNick := fmt.Sprintf("%s_", takenNick)
				c.mu.Lock()
				c.CurrentNick = newNick
				c.mu.Unlock()
				log.Printf("[%s] Nick collision for %s -> Renaming to %s", c.Network, oldNick, newNick)
				c.SendRaw(fmt.Sprintf("NICK %s", newNick))
				if c.onNickUpdated != nil {
					c.onNickUpdated(c.Network, oldNick, newNick)
				}
			}
		}
	}

	// Dispatch line to listeners
	if c.onLineReceived != nil {
		c.onLineReceived(c.Network, c.CurrentNick, line)
	}
}

// SendRaw queues a raw IRC line to be sent to the network.
func (c *Connection) SendRaw(line string) {
	select {
	case c.sendQueue <- line:
	default:
		log.Printf("[%s:%s] Send queue full; dropping line: %s", c.Network, c.CurrentNick, line)
	}
}

func (c *Connection) isRunning() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.running
}

// Close disconnects and stops the connection.
func (c *Connection) Close(quitMessage string) {
	c.mu.Lock()
	c.running = false
	if c.cancelFunc != nil {
		c.cancelFunc()
	}
	if c.conn != nil {
		if quitMessage != "" {
			_, _ = fmt.Fprintf(c.conn, "QUIT :%s\r\n", quitMessage)
		}
		_ = c.conn.Close()
	}
	c.isConnected = false
	c.mu.Unlock()
}
