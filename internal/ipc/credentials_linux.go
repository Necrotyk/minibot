// SPDX-License-Identifier: AGPL-3.0-or-later
//go:build linux

package ipc

import (
	"fmt"
	"log"
	"net"
	"os"
	"os/user"
	"strconv"
	"strings"
	"syscall"
)

// DefaultAuthorizedUsers contains the canonical allowed users for minibots IPC.
var DefaultAuthorizedUsers = []string{"minibots", "minibot", "user", "root"}

func init() {
	if envUsers := os.Getenv("MINIBOTS_AUTHORIZED_USERS"); envUsers != "" {
		for _, u := range strings.Split(envUsers, ",") {
			if trimmed := strings.TrimSpace(u); trimmed != "" {
				DefaultAuthorizedUsers = append(DefaultAuthorizedUsers, trimmed)
			}
		}
	}
}

// VerifyPeerCredentials validates that the UNIX socket peer is authorized using default allowed users.
func VerifyPeerCredentials(conn *net.UnixConn) error {
	return VerifyPeerCredentialsWithAllowedUsers(conn, DefaultAuthorizedUsers)
}

// VerifyPeerCredentialsWithAllowedUsers validates peer UID via SO_PEERCRED against an allowed username whitelist.
func VerifyPeerCredentialsWithAllowedUsers(conn *net.UnixConn, allowedUsers []string) error {
	raw, err := conn.SyscallConn()
	if err != nil {
		return fmt.Errorf("syscall conn error: %w", err)
	}

	var ucred *syscall.Ucred
	var credErr error
	err = raw.Control(func(fd uintptr) {
		ucred, credErr = syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
	})
	if err != nil {
		return fmt.Errorf("raw control error: %w", err)
	}
	if credErr != nil {
		return fmt.Errorf("getsockopt SO_PEERCRED error: %w", credErr)
	}

	currentUID := uint32(os.Getuid())
	validUIDs := map[uint32]bool{
		0:          true, // root
		currentUID: true, // running process user (e.g. minibots)
		1000:       true, // default primary operator
	}

	for _, username := range allowedUsers {
		if u, err := user.Lookup(username); err == nil {
			if id, err := strconv.ParseUint(u.Uid, 10, 32); err == nil {
				validUIDs[uint32(id)] = true
			}
		}
	}

	if !validUIDs[ucred.Uid] {
		log.Printf("[SECURITY ALERT] Rejected IPC peer connection: unauthorized UID=%d PID=%d GID=%d", ucred.Uid, ucred.Pid, ucred.Gid)
		return fmt.Errorf("unauthorized peer UID %d", ucred.Uid)
	}

	return nil
}
