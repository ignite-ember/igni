"""Watches what the process actually tries to reach, not what its source names.

`test_nothing_phones_home.py` reads string literals. It cannot see a host
assembled at runtime, a default inside a dependency, or a library that
calls home on import. This module watches the attempts themselves.

## How

A `sys.addaudithook` hook. CPython raises an audit event for every
`socket.getaddrinfo`, `socket.connect`, `socket.sendto` and
`subprocess.Popen`, from C, whatever the caller: asyncio, httpx, asyncpg,
redis, a thread pool, a dependency nobody has read. Monkeypatching
`socket.socket.connect` misses anything that held a reference before the
patch; the audit hook does not.

A destination is **internal** when it is:

- an IP address that is loopback, RFC 1918, IPv6 unique-local, or
  unspecified (`0.0.0.0`, `::`);
- `localhost`, or a name under a suffix reserved for private use and
  never delegated in public DNS: `.localhost`, `.internal` (ICANN, 2024),
  `.local` (mDNS, and Kubernetes' `cluster.local`), `.home.arpa`;
- a single-label name (`postgres`, `redis`, `vllm-0`), which is a compose
  or Kubernetes service name and resolves only inside the deployment;
- a name passed in `extra_hosts`, or listed in `IGNI_EGRESS_ALLOW_HOSTS`
  (comma-separated), for an internal service the deployment names fully.

Everything else is external, **including link-local** `169.254.0.0/16`:
that is the cloud metadata service, the one address a dependency is most
likely to probe by itself. A Unix socket is always internal.

An external attempt is recorded and refused. Refused the way an air-gapped
network refuses it: `getaddrinfo` fails as a name that does not resolve,
`connect` fails as an unreachable network. Code that tolerates being
offline carries on, which is what it would do in the customer's network.
Recording as well as refusing is what makes that visible: a caller that
swallows the error still leaves the attempt in the log, and the test
fails on the log.

## What it cannot see

- Traffic from code that does not go through CPython's `socket` module:
  a Rust or C extension with its own networking (`hf_xet`, a Rust HTTP
  client), or a child process once it is running.
- A child process's own connections. Starting one is visible, so with
  `watch_subprocesses=True` an argument that names an external URL
  (`https://host/...`, `git@host:...`) counts as an attempt. A child that
  reads its destination from a config file is not seen.
- Hooks are process-wide and cannot be removed, so a second process (a
  uvicorn started by a test) needs its own guard.
"""

from __future__ import annotations

import contextlib
import errno
import ipaddress
import os
import re
import socket
import sys
import threading
from collections.abc import Iterator

from pydantic import BaseModel, ConfigDict

_ALLOW_ENV = "IGNI_EGRESS_ALLOW_HOSTS"

# Never delegated in public DNS, so a name under one cannot leave.
_PRIVATE_SUFFIXES = (".localhost", ".internal", ".local", ".home.arpa")

_INTERNAL_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
    )
)

# `https://host`, `git://host`, `ssh://user@host` and the scp form
# `git@host:path` — the shapes a URL takes on a command line.
_URL_HOST = re.compile(
    r"\b[a-z][a-z0-9+.-]*://(?:[^@/\s]+@)?\[?([A-Za-z0-9._:%-]+?)\]?(?::\d+)?(?:[/?#\s]|$)"
)
_SCP_HOST = re.compile(r"^[A-Za-z0-9._-]+@([A-Za-z0-9._-]+):")


class EgressAttempt(BaseModel):
    """One attempt to reach something outside the customer's network."""

    model_config = ConfigDict(frozen=True)

    event: str
    destination: str
    port: int | None = None

    def describe(self) -> str:
        where = self.destination if self.port is None else f"{self.destination}:{self.port}"
        return f"{self.event} -> {where}"


class ExternalNameRefused(socket.gaierror):
    """What an air-gapped resolver says about a public name."""


class ExternalAddressRefused(OSError):
    """What an air-gapped network says about a public address."""


class InternalNetwork:
    """Decides whether a host or address is inside the deployment."""

    def __init__(self, *, extra_hosts: frozenset[str] = frozenset()) -> None:
        configured = {
            h.strip().lower() for h in os.environ.get(_ALLOW_ENV, "").split(",") if h.strip()
        }
        self._names = frozenset({"localhost"} | configured | {h.lower() for h in extra_hosts})

    def contains(self, host: str | bytes | None) -> bool:
        if host is None:
            return True  # a lookup for a local bind
        name = (host.decode() if isinstance(host, bytes) else host).strip().rstrip(".").lower()
        if not name:
            return True
        address = self._as_address(name)
        if address is not None:
            return address.is_unspecified or any(address in net for net in _INTERNAL_NETWORKS)
        return name in self._names or name.endswith(_PRIVATE_SUFFIXES) or "." not in name

    @staticmethod
    def _as_address(name: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            address = ipaddress.ip_address(name.split("%", 1)[0])
        except ValueError:
            return None
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            return address.ipv4_mapped
        return address


class EgressLog:
    """The attempts seen while one `EgressGuard.watching()` block was open."""

    def __init__(self) -> None:
        self._attempts: list[EgressAttempt] = []
        self._lock = threading.Lock()

    def add(self, attempt: EgressAttempt) -> None:
        with self._lock:
            self._attempts.append(attempt)

    @property
    def attempts(self) -> list[EgressAttempt]:
        with self._lock:
            return list(self._attempts)

    def report(self) -> str:
        seen = sorted({a.describe() for a in self.attempts})
        return (
            "igni tried to reach outside the customer network:\n"
            + "\n".join(f"  {line}" for line in seen)
            + "\n\nIn an air-gapped deployment this is a failure or a hang. If the "
            "destination is a service inside the deployment, name it in extra_hosts "
            f"or {_ALLOW_ENV}; if it is outside, the code must not try."
        )


class EgressGuard:
    """Refuses and records external connection attempts inside `watching()`.

    The audit hook is installed once per process, because Python offers no
    way to remove one, and dispatches to whichever guards are watching at
    the time. That process-wide registry is the one piece of module state
    here, and it exists because the hook it serves is process-wide too.
    """

    _watching: list[EgressGuard] = []
    _installed = False
    _install_lock = threading.Lock()
    _busy = threading.local()

    def __init__(self, *, network: InternalNetwork, watch_subprocesses: bool = False) -> None:
        self._network = network
        self._watch_subprocesses = watch_subprocesses
        self._log = EgressLog()

    @contextlib.contextmanager
    def watching(self) -> Iterator[EgressLog]:
        self._install()
        self._log = EgressLog()
        EgressGuard._watching.append(self)
        try:
            yield self._log
        finally:
            EgressGuard._watching.remove(self)

    @classmethod
    def _install(cls) -> None:
        with cls._install_lock:
            if not cls._installed:
                sys.addaudithook(cls._dispatch)
                cls._installed = True

    @classmethod
    def _dispatch(cls, event: str, args: tuple[object, ...]) -> None:
        if not cls._watching or not (
            event.startswith("socket.") or event in ("subprocess.Popen", "os.system")
        ):
            return
        if getattr(cls._busy, "on", False):
            return
        cls._busy.on = True
        refusals: list[OSError] = []
        try:
            # Every guard records before any refuses. Guards nest — the
            # suite-wide one around a test's own — and the first to raise
            # would otherwise hide the attempt from the rest.
            for guard in list(cls._watching):
                try:
                    guard._observe(event, args)
                except OSError as refusal:
                    refusals.append(refusal)
        finally:
            cls._busy.on = False
        if refusals:
            raise refusals[0]

    def _observe(self, event: str, args: tuple[object, ...]) -> None:
        if event == "socket.getaddrinfo":
            self._check_name(event, args[0], args[1])
        elif event in ("socket.gethostbyname", "socket.gethostbyname_ex", "socket.gethostbyaddr"):
            self._check_name(event, args[0], None)
        elif event in ("socket.connect", "socket.sendto"):
            self._check_socket(event, args[0], args[1])
        elif event in ("subprocess.Popen", "os.system") and self._watch_subprocesses:
            self._check_command(event, args)

    def _check_name(self, event: str, host: object, port: object) -> None:
        if not isinstance(host, (str, bytes)) or self._network.contains(host):
            return
        name = host.decode() if isinstance(host, bytes) else host
        self._log.add(EgressAttempt(event=event, destination=name, port=_port(port)))
        raise ExternalNameRefused(socket.EAI_NONAME, f"{name}: refused by the egress guard")

    def _check_socket(self, event: str, sock: object, address: object) -> None:
        if not isinstance(sock, socket.socket) or sock.family not in (
            socket.AF_INET,
            socket.AF_INET6,
        ):
            return
        if not isinstance(address, tuple) or not address:
            return
        host = address[0]
        if not isinstance(host, (str, bytes)) or self._network.contains(host):
            return
        name = host.decode() if isinstance(host, bytes) else host
        port = _port(address[1] if len(address) > 1 else None)
        self._log.add(EgressAttempt(event=event, destination=name, port=port))
        raise ExternalAddressRefused(errno.ENETUNREACH, f"{name}: refused by the egress guard")

    def _check_command(self, event: str, args: tuple[object, ...]) -> None:
        words = _command_words(args[1] if event == "subprocess.Popen" else args[0])
        for word in words:
            for host in _hosts_in(word):
                if not self._network.contains(host):
                    self._log.add(
                        EgressAttempt(event=event, destination=f"{host} ({' '.join(words)[:120]})")
                    )
                    raise ExternalAddressRefused(
                        errno.ENETUNREACH, f"{host}: refused by the egress guard"
                    )


def _port(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, (str, bytes)) and value.isdigit():
        return int(value)
    return None


def _command_words(value: object) -> list[str]:
    if isinstance(value, (str, bytes, os.PathLike)):
        return [os.fsdecode(value)]
    if isinstance(value, (list, tuple)):
        return [os.fsdecode(v) for v in value if isinstance(v, (str, bytes, os.PathLike))]
    return []


def _hosts_in(word: str) -> list[str]:
    hosts = _URL_HOST.findall(word)
    scp = _SCP_HOST.match(word)
    if scp:
        hosts.append(scp.group(1))
    return [h for h in hosts if h and not h.startswith("/")]
