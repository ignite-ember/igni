"""Linux: bubblewrap (``bwrap``), with a network namespace of its own.

The process gets:

* **network** — a new network namespace (``--unshare-net``): nothing but
  a loopback of its own, so no address on this machine or any other is
  reachable directly. :mod:`.bridge` runs first inside and listens on
  that loopback for the egress proxy and igni's own loopback ports (host
  channel, token endpoint), each relayed through a unix socket in a
  directory bound in from igni's run directory. A database the extension
  starts on its loopback is its own and nobody else's.
* **files** — the root filesystem read-only; ``/home``, ``/root``, the
  person's ``$HOME`` wherever it is, ``/run`` (sockets of the session
  bus, keyrings, Docker), ``/tmp`` and ``/var/tmp`` replaced by empty,
  read-only tmpfs; then the plan's read paths bound read-only and its
  write paths read-write on top, parents before children.
* **processes** — the same PID namespace, so the process group igni
  checks (:mod:`ember_code.extensions.host.processes`) is still
  ``bwrap``'s, igni's own child; ``--die-with-parent``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from ember_code.extensions.sandbox.plan import SandboxPlan

#: The port the proxy is reached on inside the namespace's own loopback.
#: Any number does: nothing else listens there.
PROXY_PORT = 3128

#: Directories replaced by an empty tmpfs before the grants are bound.
HIDDEN = ("/home", "/root", "/run", "/tmp", "/var/tmp")  # noqa: S108 — paths the sandbox hides, not files it writes


class Bubblewrap:
    """Writes the ``bwrap`` command line."""

    name = "Linux bubblewrap"

    @staticmethod
    def executable() -> str | None:
        return shutil.which("bwrap")

    @classmethod
    async def probe(cls) -> str | None:
        """``None`` when ``bwrap`` can make a network namespace here, else why not.

        It cannot where unprivileged user namespaces are off (some
        distributions' AppArmor policy, some containers).
        """
        bwrap = cls.executable()
        if bwrap is None:
            return "bubblewrap (bwrap) is not installed"
        true = shutil.which("true") or "/bin/true"
        try:
            proc = await asyncio.create_subprocess_exec(
                bwrap,
                "--unshare-net",
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                true,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), 10)
        except (OSError, TimeoutError) as exc:
            return f"bwrap could not run: {exc}"
        if proc.returncode != 0:
            return f"bwrap cannot create a sandbox here: {err.decode(errors='replace').strip()}"
        return None

    @staticmethod
    def socket_dir(plan: SandboxPlan) -> Path:
        assert plan.run_dir is not None
        return plan.run_dir / "net"

    @staticmethod
    def proxy_socket(plan: SandboxPlan) -> Path:
        return Bubblewrap.socket_dir(plan) / "proxy.sock"

    @staticmethod
    def relay_socket(plan: SandboxPlan, port: int) -> Path:
        return Bubblewrap.socket_dir(plan) / f"port-{port}.sock"

    @classmethod
    def command(
        cls,
        plan: SandboxPlan,
        command: str,
        args: list[str],
        *,
        cwd: Path | None,
        bridge_python: str | None = None,
        home: Path | None = None,
    ) -> tuple[str, list[str]]:
        """The ``bwrap`` argv that runs ``command`` under ``plan``.

        Args:
            bridge_python: The interpreter the bridge runs under; igni's.
            home: The person's home, hidden wherever it is.
        """
        bwrap = cls.executable() or "bwrap"
        python = bridge_python or sys.executable
        sockets = cls.socket_dir(plan)
        bridge = sockets / "bridge.py"
        shutil.copyfile(Path(__file__).with_name("bridge.py"), bridge)

        argv: list[str] = [
            "--die-with-parent",
            "--unshare-net",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
        ]
        hidden = list(HIDDEN)
        person = home or Path(os.path.expanduser("~"))
        if not any(str(person).startswith(h + "/") or str(person) == h for h in hidden):
            hidden.append(str(person))
        hidden = [path for path in hidden if Path(path).exists()]
        for path in hidden:
            argv += ["--tmpfs", path]

        # The bridge's interpreter must be readable wherever it lives.
        read = list(plan.read) + SandboxPlan.runtime_paths(python, dict(os.environ))
        # Read-only, then read-write over it, then the sockets; mounted
        # parents first so a child's mount is not hidden by its parent's.
        mounts: dict[str, str] = dict.fromkeys(map(str, read), "--ro-bind-try")
        mounts.update(dict.fromkeys(map(str, plan.write), "--bind-try"))
        mounts[str(sockets)] = "--bind"
        for path in sorted(mounts, key=lambda p: (len(Path(p).parts), p)):
            argv += [mounts[path], path, path]

        # The empty tmpfs over each hidden directory read-only too, once
        # the grants are mounted inside it: nothing to read, nowhere to write.
        for path in hidden:
            argv += ["--remount-ro", path]

        if cwd is not None:
            argv += ["--chdir", str(cwd)]

        forwards: list[str] = ["--forward", f"{PROXY_PORT}={cls.proxy_socket(plan)}"]
        for port in plan.loopback_ports:
            forwards += ["--forward", f"{port}={cls.relay_socket(plan, port)}"]
        argv += ["--", python, "-I", str(bridge), *forwards, "--", command, *args]
        return bwrap, argv


__all__ = ["PROXY_PORT", "Bubblewrap"]
