// SPDX-License-Identifier: AGPL-3.0-or-later
//go:build !linux

package ipc

import "net"

// VerifyPeerCredentials stub for non-linux compilation.
func VerifyPeerCredentials(conn *net.UnixConn) error {
	return nil
}

// VerifyPeerCredentialsWithAllowedUsers stub for non-linux compilation.
func VerifyPeerCredentialsWithAllowedUsers(conn *net.UnixConn, allowedUsers []string) error {
	return nil
}
