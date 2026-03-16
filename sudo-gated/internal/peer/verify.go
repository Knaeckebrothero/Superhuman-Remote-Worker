// Package peer provides SO_PEERCRED verification for Unix socket connections.
//
// When the C plugin connects to the daemon's Unix socket, the daemon uses
// SO_PEERCRED to get the connecting process's PID, then reads /proc/{pid}/exe
// to verify the caller is /usr/bin/sudo.
package peer

import (
	"fmt"
	"net"
	"os"
	"path/filepath"

	"golang.org/x/sys/unix"
)

// AllowedBinaries is the set of executable paths that are permitted to
// connect to the daemon. In production this is just /usr/bin/sudo.
var AllowedBinaries = map[string]bool{
	"/usr/bin/sudo": true,
}

// Verify checks that the peer on the other end of conn is an allowed binary.
// Returns the PID of the connecting process, or an error if verification fails.
//
// When skipVerify is true, the function still returns the PID but skips the
// binary check. This is useful for development/testing with mock_plugin.py.
func Verify(conn net.Conn, skipVerify bool) (int32, error) {
	uc, ok := conn.(*net.UnixConn)
	if !ok {
		return 0, fmt.Errorf("not a Unix connection")
	}

	raw, err := uc.SyscallConn()
	if err != nil {
		return 0, fmt.Errorf("get syscall conn: %w", err)
	}

	var cred *unix.Ucred
	var credErr error

	err = raw.Control(func(fd uintptr) {
		cred, credErr = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
	})
	if err != nil {
		return 0, fmt.Errorf("raw control: %w", err)
	}
	if credErr != nil {
		return 0, fmt.Errorf("SO_PEERCRED: %w", credErr)
	}

	if skipVerify {
		return cred.Pid, nil
	}

	// Read /proc/{pid}/exe to get the binary path.
	exePath := fmt.Sprintf("/proc/%d/exe", cred.Pid)
	target, err := os.Readlink(exePath)
	if err != nil {
		return cred.Pid, fmt.Errorf("readlink %s: %w", exePath, err)
	}

	// Resolve any symlinks in the allowed binaries for comparison.
	resolved, err := filepath.EvalSymlinks(target)
	if err != nil {
		resolved = target
	}

	if !AllowedBinaries[resolved] && !AllowedBinaries[target] {
		return cred.Pid, fmt.Errorf("unauthorized binary: %s (pid %d)", target, cred.Pid)
	}

	return cred.Pid, nil
}
