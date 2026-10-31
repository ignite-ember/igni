"""Inside a Linux sandbox: loopback ports that lead out through unix sockets.

Runs as the first process inside ``bwrap --unshare-net``, where the only
network is a loopback of the sandbox's own. For each ``--forward
PORT=SOCKET`` it listens on that namespace's ``127.0.0.1:PORT`` and
relays every connection to ``SOCKET`` — a unix socket igni listens on
outside, bound into the sandbox: the egress proxy, or a relay to one of
igni's own loopback ports (host channel, token endpoint). Then it runs
the extension's command with the same stdio, passes signals on, and exits
with its status.

Standard library only, and no imports from igni: it runs under igni's
interpreter with ``-I``, from a copy in the run directory.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)


def _serve(listener: socket.socket, target: str) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.connect(target)
        except OSError:
            client.close()
            continue
        for a, b in ((client, upstream), (upstream, client)):
            threading.Thread(target=_pump, args=(a, b), daemon=True).start()


def main(argv: list[str]) -> int:
    if "--" not in argv:
        print("usage: bridge.py [--forward PORT=SOCKET]... -- command [args]", file=sys.stderr)
        return 2
    split = argv.index("--")
    options, command = argv[:split], argv[split + 1 :]
    forwards: list[tuple[int, str]] = []
    it = iter(options)
    for option in it:
        if option != "--forward":
            print(f"bridge.py: unknown option {option}", file=sys.stderr)
            return 2
        port, _, path = next(it).partition("=")
        forwards.append((int(port), path))
    for port, path in forwards:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(64)
        threading.Thread(target=_serve, args=(listener, path), daemon=True).start()
    if not command:
        return 2
    child = subprocess.Popen(command)  # noqa: S603 — the extension's own argv
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda signum, _frame: child.send_signal(signum))
    code = child.wait()
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main(sys.argv[1:]))
