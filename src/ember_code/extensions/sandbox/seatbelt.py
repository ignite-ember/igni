"""macOS: Seatbelt, through ``/usr/bin/sandbox-exec`` and a generated profile.

``sandbox-exec`` is deprecated and still shipped; Chrome and other
sandboxes for developer tools use the same interface. The profile denies
by default and allows:

* **processes** — fork and exec, signals and process info within the
  same sandbox, sysctl reads, POSIX shared memory and semaphores, and a
  short list of system services a process needs to start (directory
  lookups, logging, preferences, notifications). Not the keychain, not
  the network daemons, not DNS.
* **files** — read everything **except every home directory** under
  ``/Users`` and the per-user temp and cache directories under
  ``/private/var/folders``, which are denied apart from the plan's read
  paths; whether a path there exists, and its metadata, stay visible so
  paths resolve (a JVM will not start otherwise), its contents and
  listings do not. Write only the plan's write paths and the standard
  devices.
* **network** — this machine's loopback only: outbound to
  ``localhost:*`` and binding to it, for the egress proxy, igni's host
  channel and token endpoint, and a database the extension runs itself
  (CodeIndex's Neo4j). Seatbelt cannot filter loopback by port for a
  server the process starts on a port it picks, so on macOS the process
  can reach other services listening on this machine — Seatbelt's
  ``localhost`` is this machine, its own LAN addresses included — except
  igni's own: every port igni listens on when the process starts (the
  backend's socket, other extensions' proxies and pages) is denied but
  the host channel, the token endpoint and its own proxy. Every
  other host, by name or by IP, is denied by the kernel: ``EPERM`` on
  ``connect``, and no DNS. Unix sockets only inside its own directories.
* **launching** — not: LaunchServices, Apple Events and the pasteboard
  are not among the services it may reach, so neither ``open`` (``-R``
  and ``-a`` included) nor ``osascript`` can hand anything to another
  application; executing ``open``, ``osascript``, ``osacompile`` and
  ``lsappinfo`` is refused outright, so they fail visibly (``open -R``
  otherwise exits 0 having reached nothing).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from pathlib import Path

from ember_code.extensions.sandbox.plan import SandboxPlan

logger = logging.getLogger(__name__)

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

#: Denied to read but for the plan's read paths: every home directory,
#: and the per-user temp and cache directories other programs share
#: (``$TMPDIR`` is under ``/private/var/folders``).
HOME_ROOTS = ("/Users", "/private/var/folders")

#: System services a process needs to start and run. Deliberately not:
#: com.apple.SecurityServer and the securityd/keychain family, trustd
#: (fetches on a caller's behalf), mDNSResponder and dnssd (DNS is a
#: channel out), nsurlsessiond and cfnetwork agents (downloads on a
#: caller's behalf).
MACH_SERVICES = (
    "com.apple.bsd.dirhelper",
    "com.apple.cfprefsd.agent",
    "com.apple.cfprefsd.daemon",
    "com.apple.diagnosticd",
    "com.apple.FSEvents",
    "com.apple.logd",
    "com.apple.logd.events",
    "com.apple.lsd.mapdb",
    "com.apple.secinitd",
    "com.apple.system.DirectoryService.libinfo_v1",
    "com.apple.system.logger",
    "com.apple.system.notification_center",
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership",
    "com.apple.SystemConfiguration.configd",
)

#: Commands that hand work to another application, refused outright.
LAUNCHERS = " ".join(
    f'(literal "{path}")'
    for path in ("/usr/bin/open", "/usr/bin/osascript", "/usr/bin/osacompile", "/usr/bin/lsappinfo")
)

#: Devices a process writes to.
DEVICES = ("/dev/null", "/dev/zero", "/dev/tty", "/dev/dtracehelper", "/dev/random", "/dev/urandom")


def _quote(path: Path | str) -> str:
    text = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _subpaths(paths: list[Path]) -> str:
    return " ".join(f"(subpath {_quote(p)})" for p in paths)


class Seatbelt:
    """Writes the profile and the ``sandbox-exec`` command line."""

    name = "macOS Seatbelt"

    @staticmethod
    def found() -> bool:
        return Path(SANDBOX_EXEC).is_file()

    @staticmethod
    async def probe() -> str | None:
        """``None`` when ``sandbox-exec`` can confine a process here, else why not.

        It cannot when igni itself already runs in a sandbox, which
        does not allow applying another.
        """
        if not Seatbelt.found():
            return f"{SANDBOX_EXEC} is not on this machine"
        true = shutil.which("true") or "/usr/bin/true"
        try:
            proc = await asyncio.create_subprocess_exec(
                SANDBOX_EXEC,
                "-p",
                "(version 1)(allow default)(deny network*)",
                true,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), 10)
        except (OSError, TimeoutError) as exc:
            return f"{SANDBOX_EXEC} could not run: {exc}"
        if proc.returncode != 0:
            return f"{SANDBOX_EXEC} failed: {err.decode(errors='replace').strip()}"
        return None

    @staticmethod
    async def listening_ports(pid: int | None = None) -> set[int]:
        """The TCP ports a process (igni, by default) listens on, by ``lsof``.

        Empty when ``lsof`` cannot say: the profile then falls back to
        allowing all of this machine's loopback, as documented.
        """
        lsof = shutil.which("lsof") or "/usr/sbin/lsof"
        try:
            proc = await asyncio.create_subprocess_exec(
                lsof,
                "-nP",
                "-a",
                "-p",
                str(pid if pid is not None else os.getpid()),
                "-iTCP",
                "-sTCP:LISTEN",
                "-Fn",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
        except (OSError, TimeoutError):
            logger.warning("lsof could not list igni's own ports; they stay reachable")
            return set()
        ports: set[int] = set()
        for line in out.decode(errors="replace").splitlines():
            if line.startswith("n") and ":" in line:
                with contextlib.suppress(ValueError):
                    ports.add(int(line.rsplit(":", 1)[1]))
        return ports

    @staticmethod
    def profile(plan: SandboxPlan) -> str:
        reads = _subpaths(plan.read)
        writes = _subpaths(plan.write)
        mach = " ".join(f'(global-name "{name}")' for name in MACH_SERVICES)
        devices = " ".join(f"(literal {_quote(d)})" for d in DEVICES)
        homes = " ".join(f"(subpath {_quote(h)})" for h in HOME_ROOTS)
        lines = [
            "(version 1)",
            f";; igni extension sandbox: {plan.extension_id}",
            "(deny default)",
            ";; processes",
            "(allow process-exec process-fork)",
            # Handing work to another application is a way out of the
            # sandbox; LaunchServices and Apple Events are already not
            # reachable, and these say so with an error rather than exit 0
            # (``open -R`` reports success when it reaches nothing).
            f"(deny process-exec {LAUNCHERS})",
            "(deny appleevent-send)",
            "(allow signal (target same-sandbox))",
            "(allow process-info* (target same-sandbox))",
            "(allow mach-priv-task-port (target same-sandbox))",
            "(allow sysctl-read)",
            "(allow system-info)",
            "(allow system-socket)",
            "(allow ipc-posix-shm* ipc-posix-sem*)",
            "(allow user-preference-read)",
            '(allow iokit-open (iokit-user-client-class "RootDomainUserClient"))',
            f"(allow mach-lookup {mach})",
            "(allow pseudo-tty)",
            ";; files: read all but home directories, then the grants",
            "(allow file-read* file-map-executable file-test-existence)",
            f"(deny file-read* file-map-executable file-test-existence {homes})",
            f"(allow file-read-metadata file-test-existence {homes})",
            # CoreFoundation's text encoding, read from the account's home.
            '(allow file-read* (regex #"^/Users/[^/]+/\\.CFUserTextEncoding$"))',
        ]
        if reads:
            lines.append(f"(allow file-read* file-map-executable file-test-existence {reads})")
        lines += [
            ";; files: write only the grants",
            f'(allow file-write-data file-ioctl {devices} (subpath "/dev/fd"))',
            '(allow file-read* file-write* file-ioctl (literal "/dev/ptmx") (regex #"^/dev/ttys[0-9]+$"))',
        ]
        if writes:
            lines.append(f"(allow file-write* {writes})")
        lines += [
            ";; network: loopback only; everything else through igni's proxy",
            '(allow network-outbound (remote ip "localhost:*"))',
            '(allow network-bind network-inbound (local ip "localhost:*"))',
        ]
        if writes:
            lines.append(f"(allow network-bind network-outbound network-inbound {writes})")
        if plan.denied_ports:
            # igni's own listeners it was not granted — the backend's
            # socket, other extensions' proxies and pages — denied after
            # the loopback allow, which they would otherwise fall under.
            lines.append(";; igni's own loopback listeners, but for its grants")
            remotes = " ".join(f'(remote ip "localhost:{p}")' for p in plan.denied_ports)
            lines.append(f"(deny network-outbound {remotes})")
        return "\n".join(lines) + "\n"

    @classmethod
    def command(cls, plan: SandboxPlan, command: str, args: list[str]) -> tuple[str, list[str]]:
        """Write the profile into the run dir and return the wrapped argv."""
        assert plan.run_dir is not None
        profile = plan.run_dir / "profile.sb"
        profile.write_text(cls.profile(plan))
        return SANDBOX_EXEC, ["-f", str(profile), command, *args]


__all__ = ["SANDBOX_EXEC", "Seatbelt"]
