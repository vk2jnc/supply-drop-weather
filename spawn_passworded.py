#!/usr/bin/env python3
"""spawn_passworded.py — run a command that prompts for a password on a PTY.

Usage: spawn_passworded.py <password> <cmd...>
Feeds the password each time the child shows a bare password prompt (line
ending in "password" + optional colon), up to 3 times. Exits with the
child's exit code.
"""
import os
import pty
import select
import signal
import sys
import time


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: spawn_passworded.py <password> <cmd...>", file=sys.stderr)
        return 2
    password = sys.argv[1]
    cmd = sys.argv[2:]

    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execvp(cmd[0], cmd)
        except Exception:
            os._exit(127)

    sent = 0
    buf = b""
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            r, _, _ = select.select([fd], [], [], 1.0)
        except InterruptedError:
            continue
        if fd in r:
            try:
                data = os.read(fd, 4096)
            except OSError:
                # Linux PTY: reading the master after the child exits yields EIO.
                # That's the normal "child finished" signal — fall through and
                # reap it for its real exit code (don't treat as timeout).
                data = b""
            if not data:
                done, status = os.waitpid(pid, 0)
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
                return 128 + os.WTERMSIG(status)
            buf += data
            sys.stderr.write(data.decode(errors="replace"))  # mirror output
            sys.stderr.flush()
            # A prompt we can answer: last line is a bare "password" (case-insensitive,
            # optionally followed by ':' and/or spaces)
            lines = buf.split(b"\n")
            if sent < 3:
                tail = lines[-2] if lines[-1].strip() == b"" else lines[-1]
                stripped = tail.lower().rstrip().rstrip(b":")
                if stripped.strip().endswith(b"password"):
                    os.write(fd, password.encode() + b"\n")
                    sent += 1
                    buf = b""
                    deadline = time.time() + 30
                    continue
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            if os.WIFEXITED(status):
                return os.WEXITSTATUS(status)
            return 128 + os.WTERMSIG(status)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)
    return 124


if __name__ == "__main__":
    sys.exit(main())
