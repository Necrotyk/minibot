// SPDX-License-Identifier: AGPL-3.0-or-later
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/Necrotyk/minibot/internal/config"
	"github.com/Necrotyk/minibot/internal/ipc"
	"github.com/Necrotyk/minibot/internal/irc"
)

func main() {
	configPathFlag := flag.String("config", "", "Path to config.json (defaults to $MINIBOTS_CONFIG or /opt/minibots/config.json)")
	ipcSocketFlag := flag.String("ipc-sock", "", "Path to UNIX IPC socket (defaults to $MINIBOTS_IPC_SOCK or data/minibots_ipc.sock)")
	verboseFlag := flag.Bool("verbose", false, "Enable verbose logging")
	flag.Parse()

	if *verboseFlag {
		log.Println("[Minibots-Conn] Verbose logging enabled")
	}

	log.Println("[Minibots-Conn] Starting Minibots Go Connection Daemon & Multiplexer...")

	// 1. Resolve IPC socket path
	socketPath := *ipcSocketFlag
	if socketPath == "" {
		socketPath = os.Getenv("MINIBOTS_IPC_SOCK")
	}
	if socketPath == "" {
		socketPath = "data/minibots_ipc.sock"
	}

	// 2. Load Configuration
	cfg, err := config.LoadConfig(*configPathFlag)
	if err != nil {
		log.Printf("[Minibots-Conn] Warning loading configuration: %v. Using defaults.", err)
	}

	// 3. Initialize IRC Manager and IPC Server
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var ipcServer *ipc.Server
	mgr := irc.NewManager(cfg.Servers, func(b ipc.OutboundBroadcast) {
		if ipcServer != nil {
			ipcServer.Broadcast(b)
		}
	})

	ipcServer = ipc.NewServer(socketPath, ipc.DefaultAuthorizedUsers, mgr)

	// 4. Start IPC Server
	if err := ipcServer.Start(); err != nil {
		log.Fatalf("[Minibots-Conn] Fatal: failed to start IPC server on %s: %v", socketPath, err)
	}

	// 5. Start IRC Manager
	mgr.Start(ctx)
	log.Printf("[Minibots-Conn] Connection daemon running with %d IRC network(s)", len(cfg.Servers))

	// 6. Signal handling
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)

	for sig := range sigChan {
		if sig == syscall.SIGHUP {
			log.Println("[Minibots-Conn] Received SIGHUP. Reloading configuration...")
			newCfg, err := config.LoadConfig(*configPathFlag)
			if err != nil {
				log.Printf("[Minibots-Conn] Failed reloading config: %v", err)
			} else {
				log.Printf("[Minibots-Conn] Reloaded config successfully (%d networks)", len(newCfg.Servers))
				for name, srv := range newCfg.Servers {
					_ = mgr.HandleCommand(ipc.InboundCommand{
						Type:    "add_server",
						Network: name,
						Host:    srv.Host,
						Port:    srv.Port,
						SSL:     srv.SSL,
					})
				}
			}
			continue
		}

		log.Printf("[Minibots-Conn] Received signal %v. Initiating graceful shutdown...", sig)
		break
	}

	// 7. Graceful teardown
	cancel()
	mgr.Stop("Minibots connection daemon shutting down")
	_ = ipcServer.Close()
	log.Println("[Minibots-Conn] Daemon stopped cleanly.")
}
