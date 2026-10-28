"""Measure igni across a simulated resume, and what it dirties while idle.

``docs/CLOUD_RESUME_TODO.md`` items 1 and 2. Linux only (it reads
``/proc/<pid>/clear_refs``, ``smaps`` and ``pagemap``; run as root, or
as igni's user with ``CAP_SYS_ADMIN`` for the pagemap's flags).

Two ways to run it:

* **Its own igni** (a Linux container): starts the backend in the
  cloud profile against a fake model, binds it as the node agent would
  (token, ``producer.env``), runs a short session, lets it idle, then
  plays restores — stop the process, clear its pages' referenced bits,
  continue it, write ``restored``, connect at once — and times each
  step from the client's side.
* **An igni already running** (on the image, ``--pid`` and ``--ws``):
  the same measurements, against that process; ``--restored`` names its
  run directory's ``restored`` file to write. Without ``--simulate`` the
  probe only measures the idle window and prints the timeline igni
  itself logged for the last real restore (``resume timeline`` lines).

Per resume it prints: the WebSocket handshake (connect → ``welcome``), the
first RPC, the first database RPC, the kB of igni's memory referenced
from the restore to the last answer (what a cold resume must read), and
its CPU time. For the idle window: the pages dirtied (soft-dirty), by
mapping. igni's own ``resume timeline`` (each after-restore step, the
first connection, the first answer, with page faults) is in its log.

    python scripts/measure_resume.py --out r.json [--database-url URL] [--idle 60] [--resumes 10]
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import signal
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import click
from sqlalchemy import create_engine, text
from websockets.asyncio.client import connect


class FakeModel(BaseHTTPRequestHandler):
    """Answers every chat completion with a streamed "hi"."""

    def log_message(self, *_: Any) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        chunk = {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "fake",
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": None}
            ],
        }
        done = dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}])
        for c in (chunk, done):
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


class ProcessPages:
    """One process's pages, through ``/proc``: referenced and soft-dirty."""

    PAGE = os.sysconf("SC_PAGE_SIZE")
    #: ``clear_refs`` values: referenced bits, soft-dirty bits.
    CLEAR_REFERENCED = "1"
    CLEAR_SOFT_DIRTY = "4"
    _SOFT_DIRTY = 1 << 55
    _PRESENT = 1 << 63
    _MAP = re.compile(r"^([0-9a-f]+)-([0-9a-f]+) \S+ \S+ \S+ \S+\s*(.*)$")

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def clear(self, what: str) -> None:
        Path(f"/proc/{self.pid}/clear_refs").write_text(what)

    def rss_kb(self) -> int:
        for line in Path(f"/proc/{self.pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Rss:"):
                return int(line.split()[1])
        return 0

    def referenced_kb(self) -> int:
        for line in Path(f"/proc/{self.pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Referenced:"):
                return int(line.split()[1])
        return 0

    def referenced_by_mapping(self, top: int = 12) -> dict[str, int]:
        counts: Counter[str] = Counter()
        name = ""
        for line in Path(f"/proc/{self.pid}/smaps").read_text().splitlines():
            head = self._MAP.match(line)
            if head:
                name = self._label(head.group(3))
            elif line.startswith("Referenced:"):
                counts[name] += int(line.split()[1])
        return dict(counts.most_common(top))

    def soft_dirty(self, top: int = 12) -> tuple[int, dict[str, int]]:
        """Pages written since the last clear: total, and kB by mapping."""
        counts: Counter[str] = Counter()
        total = 0
        maps = Path(f"/proc/{self.pid}/maps").read_text().splitlines()
        with open(f"/proc/{self.pid}/pagemap", "rb") as pagemap:
            for line in maps:
                head = self._MAP.match(line)
                if not head:
                    continue
                start, end = int(head.group(1), 16), int(head.group(2), 16)
                label = self._label(head.group(3))
                if label == "[vsyscall]":
                    continue
                pages = (end - start) // self.PAGE
                pagemap.seek(start // self.PAGE * 8)
                data = pagemap.read(pages * 8)
                dirty = 0
                for (entry,) in struct.iter_unpack("<Q", data):
                    if entry & self._PRESENT and entry & self._SOFT_DIRTY:
                        dirty += 1
                if dirty:
                    counts[label] += dirty * self.PAGE // 1024
                    total += dirty
        return total, dict(counts.most_common(top))

    def contents(self) -> dict[int, tuple[str, int]]:
        """Each present page of a writable private mapping: its address, mapping and checksum.

        Where the kernel has no soft-dirty bits (``CONFIG_MEM_SOFT_DIRTY``
        off, as in Docker Desktop's), two of these, compared, are the
        pages that changed — what a suspend writes as the producer's own.
        """
        found: dict[int, tuple[str, int]] = {}
        maps = Path(f"/proc/{self.pid}/maps").read_text().splitlines()
        with (
            open(f"/proc/{self.pid}/pagemap", "rb") as pagemap,
            open(f"/proc/{self.pid}/mem", "rb", buffering=0) as mem,
        ):
            for line in maps:
                head = self._MAP.match(line)
                if not head:
                    continue
                perms = line.split()[1]
                label = self._label(head.group(3))
                if "w" not in perms or "p" not in perms or label in ("[vvar]", "[vsyscall]"):
                    continue
                start, end = int(head.group(1), 16), int(head.group(2), 16)
                pages = (end - start) // self.PAGE
                pagemap.seek(start // self.PAGE * 8)
                entries = struct.unpack(f"<{pages}Q", pagemap.read(pages * 8))
                for index, entry in enumerate(entries):
                    if not entry & self._PRESENT:
                        continue
                    address = start + index * self.PAGE
                    try:
                        mem.seek(address)
                        page = mem.read(self.PAGE)
                    except OSError:
                        continue
                    found[address] = (label, zlib.crc32(page))
        return found

    @staticmethod
    def compare(
        before: dict[int, tuple[str, int]], after: dict[int, tuple[str, int]], top: int = 12
    ) -> dict[str, Any]:
        """``after`` against ``before``: base pages written to, and pages new since.

        A base page written to is one a snapshot of the warm base had and
        the producer's VM now holds a copy of; a new page was not in the
        base at all.
        """
        modified: Counter[str] = Counter()
        new: Counter[str] = Counter()
        for address, (label, checksum) in after.items():
            earlier = before.get(address)
            if earlier is None:
                new[label] += 1
            elif earlier[1] != checksum:
                modified[label] += 1
        kb = ProcessPages.PAGE // 1024
        return {
            "changed_kb": (sum(modified.values()) + sum(new.values())) * kb,
            "base_pages_written_kb": sum(modified.values()) * kb,
            "new_pages_kb": sum(new.values()) * kb,
            "base_pages_written_by_mapping": {k: v * kb for k, v in modified.most_common(top)},
            "new_pages_by_mapping": {k: v * kb for k, v in new.most_common(top)},
        }

    @staticmethod
    def save(pages: dict[int, tuple[str, int]], path: Path) -> None:
        with path.open("w") as out:
            for address, (label, checksum) in pages.items():
                out.write(f"{address:x} {checksum:x} {label}\n")

    @staticmethod
    def changed(
        before: dict[int, tuple[str, int]], after: dict[int, tuple[str, int]], top: int = 12
    ) -> tuple[int, dict[str, int]]:
        counts: Counter[str] = Counter()
        for address, (label, checksum) in after.items():
            earlier = before.get(address)
            if earlier is None or earlier[1] != checksum:
                counts[label] += 1
        total = sum(counts.values())
        return total, {k: v * ProcessPages.PAGE // 1024 for k, v in counts.most_common(top)}

    def cpu_seconds(self) -> float:
        fields = Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        return (int(fields[11]) + int(fields[12])) / ticks

    @staticmethod
    def _label(path: str) -> str:
        path = path.strip()
        if not path:
            return "[anon]"
        if path.startswith("["):
            return path
        if "site-packages/" in path:
            return "site-packages/" + path.split("site-packages/", 1)[1].split("/", 1)[0]
        return path


class ModelChoice:
    """The model igni is given: the fake one here, or a real one by URL.

    A real model's key is read by igni from the variable ``key_env``
    names (``api_key_env``), so it never appears in a file, an argument
    or this script's output.
    """

    def __init__(
        self,
        *,
        fake_url: str,
        url: str | None = None,
        model_id: str | None = None,
        key_env: str | None = None,
        context_window: int = 200000,
        repo_url: str | None = None,
    ) -> None:
        self.fake_url = fake_url
        self.url = url
        self.model_id = model_id
        self.key_env = key_env
        self.context_window = context_window
        self.repo_url = repo_url

    @property
    def real(self) -> bool:
        return bool(self.url and self.model_id)

    def managed_settings(self) -> dict[str, Any]:
        if self.real:
            entry: dict[str, Any] = {
                "provider": "openai_like",
                "model_id": self.model_id,
                "url": self.url,
                "api_key_env": self.key_env,
                "context_window": self.context_window,
            }
        else:
            entry = {
                "provider": "openai_like",
                "model_id": "fake",
                "url": self.fake_url,
                "api_key": "x",
                "context_window": 32000,
            }
        return {
            "models": {"default": "probe", "registry": {"probe": entry}},
            # Nobody is there to approve: a run edits files and runs
            # commands as it would with the person saying yes.
            "permissions": {
                "file_write": "allow",
                "shell_execute": "allow",
                "git_push": "deny",
                "git_destructive": "deny",
            },
        }


class ProcessTree:
    """igni and everything it started: the memory the VM holds for them."""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def pids(self) -> list[int]:
        children: dict[int, list[int]] = {}
        for stat in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = stat.read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            children.setdefault(int(fields[1]), []).append(int(stat.parent.name))
        found, stack = [], [self.pid]
        while stack:
            pid = stack.pop()
            found.append(pid)
            stack.extend(children.get(pid, []))
        return found

    @staticmethod
    def rss_kb(pid: int) -> int:
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        except OSError:
            pass
        return 0

    def sample(self) -> tuple[int, int]:
        """igni's RSS, and the whole tree's."""
        pids = self.pids()
        return self.rss_kb(self.pid), sum(self.rss_kb(pid) for pid in pids)


class Igni:
    """An igni backend started here, in the cloud profile, as a warm base starts."""

    def __init__(
        self, *, database_url: str | None, port: int, env: dict[str, str] | None = None
    ) -> None:
        self.extra_env = env or {}
        self.tmp = Path(tempfile.mkdtemp(prefix="resume-"))
        self.run_dir = self.tmp / "run"
        self.project = self.tmp / "project"
        for path in (self.run_dir, self.project, self.tmp / "home"):
            path.mkdir()
        self.database_url = database_url or f"sqlite:///{self.tmp / 'shared.db'}"
        self.port = port
        self.log = self.tmp / "igni.log"
        self.process: subprocess.Popen[bytes] | None = None

    def start(self, model: ModelChoice) -> None:
        (self.run_dir / "managed-settings.yaml").write_text(json.dumps(model.managed_settings()))
        if model.repo_url:
            # The project igni works in: a real repository, cloned fresh.
            subprocess.run(
                ["git", "clone", "--quiet", "--depth", "50", model.repo_url, str(self.project)],
                check=True,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("IGNI_") or k in ("IGNI_RESUME_PAGES",)
        }
        env.update(
            IGNI_PROFILE="cloud", IGNI_RUN_DIR=str(self.run_dir), HOME=str(self.tmp / "home")
        )
        env.update(self.extra_env)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ember_code.backend",
                "--ws-port",
                str(self.port),
                "--project-dir",
                str(self.project),
            ],
            env=env,
            stdout=self.log.open("wb"),
            stderr=subprocess.STDOUT,
        )

    def wait_ready(self, timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        while not (self.run_dir / "ready").exists():
            assert self.process is not None and self.process.poll() is None, self.log.read_text()
            if time.monotonic() > deadline:
                raise TimeoutError("igni never said ready")
            time.sleep(0.01)

    def hand_over(self) -> None:
        def part(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

        claims = {"sub": "u-ada", "org": "org-acme", "exp": int(time.time()) + 86400}
        (self.run_dir / "token").write_text(
            f"{part(b'{}')}.{part(json.dumps(claims).encode())}.sig"
        )
        staged = self.run_dir / ".producer.env"
        staged.write_text(
            f"IGNI_ACCESS_TOKEN_FILE={self.run_dir / 'token'}\nIGNI_ORG=org-acme\n"
            "IGNI_PRODUCER=person:u-ada\nIGNI_PRODUCER_INCARNATION=m-1\n"
            f"IGNI_DATABASE_URL={self.database_url}\n"
        )
        os.replace(staged, self.run_dir / "producer.env")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()


class Window:
    """A client: RPCs by id, everything else skipped."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._n = 0

    async def rpc(self, method: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        self._n += 1
        request_id = f"probe-{self._n}"
        await self.ws.send(
            json.dumps(
                {"type": "rpc_request", "id": request_id, "method": method, "args": args or {}}
            )
        )
        while True:
            message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=30))
            if message.get("id") == request_id:
                return message

    async def turn(self, text: str) -> str:
        await self.ws.send(json.dumps({"type": "user_message", "id": f"u{self._n}", "text": text}))
        last = ""
        while last not in ("stream_end", "run_error"):
            last = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=60)).get("type")
        return last

    async def chats(self, task: str, sessions: list[str], timeout: float) -> dict[str, str]:
        """The same task in each session at once; each session's last event type."""
        for number, session in enumerate(sessions):
            await self.ws.send(
                json.dumps(
                    {
                        "type": "user_message",
                        "id": f"chat-{number}",
                        "session_id": session,
                        "text": task,
                    }
                )
            )
        ended: dict[str, str] = {}
        deadline = time.monotonic() + timeout
        while len(ended) < len(sessions):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=remaining))
            kind, session = message.get("type"), message.get("session_id", "")
            if session in sessions and kind in ("stream_end", "run_error"):
                ended[session] = kind
        return {session: ended.get(session, "timed out") for session in sessions}


class AgentRunProbe:
    """A real agent run — or several at once — and the memory it takes."""

    def __init__(self, *, pid: int, url: str) -> None:
        self.tree = ProcessTree(pid)
        self.pages = ProcessPages(pid)
        self.url = url

    async def run(self, task: str, chats: int, timeout: float) -> dict[str, Any]:
        sessions = [f"probe-chat-{n}" for n in range(chats)]
        peak_igni = peak_tree = 0
        done = asyncio.Event()

        async def sample() -> None:
            nonlocal peak_igni, peak_tree
            while not done.is_set():
                igni, tree = await asyncio.to_thread(self.tree.sample)
                peak_igni, peak_tree = max(peak_igni, igni), max(peak_tree, tree)
                await asyncio.sleep(0.2)

        before = self.pages.rss_kb()
        sampler = asyncio.create_task(sample())
        started = time.perf_counter()
        async with connect(self.url, max_size=None) as ws:
            await ws.recv()
            ended = await Window(ws).chats(task, sessions, timeout)
        took = time.perf_counter() - started
        done.set()
        await sampler
        return {
            "chats": chats,
            "seconds": round(took, 1),
            "ended": ended,
            "igni_rss_before_kb": before,
            "igni_rss_peak_kb": peak_igni,
            "tree_rss_peak_kb": peak_tree,
            "igni_rss_after_kb": self.pages.rss_kb(),
        }


class PauseProbe:
    """igni's before-suspend step: connections held, closed, and the first query after."""

    HOOK = (
        Path(__file__).resolve().parents[1] / "packages/igni-cloud/hooks/before-suspend.d/50-igni"
    )

    def __init__(self, *, url: str, run_dir: Path, database_url: str) -> None:
        self.url = url
        self.run_dir = run_dir
        self.database_url = database_url

    def connections(self) -> int:
        """igni's connections to the database, as the server counts them."""
        engine = create_engine(self.database_url.replace("postgresql://", "postgresql+psycopg://"))
        try:
            with engine.connect() as c:
                return int(
                    c.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE backend_type = "
                            "'client backend' AND datname = current_database() "
                            "AND pid <> pg_backend_pid()"
                        )
                    )
                )
        finally:
            engine.dispose()

    async def run(self, rounds: int) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        for _ in range(rounds):
            async with connect(self.url, max_size=None) as ws:
                await ws.recv()
                window = Window(ws)
                await window.rpc("list_sessions")  # a connection is held
                held = self.connections()
                started = time.perf_counter()
                hook = await asyncio.create_subprocess_exec(
                    str(self.HOOK), env={**os.environ, "IGNI_RUN_DIR": str(self.run_dir)}
                )
                await hook.wait()
                hook_ms = (time.perf_counter() - started) * 1000
                paused = self.connections()
                answer = json.loads((self.run_dir / "suspend-ready").read_text())
                t0 = time.perf_counter()
                await window.rpc("list_sessions")
                first = (time.perf_counter() - t0) * 1000
                t1 = time.perf_counter()
                await window.rpc("list_sessions")
                warm = (time.perf_counter() - t1) * 1000
            out.append(
                {
                    "held_before": held,
                    "held_paused": paused,
                    "hook_ms": round(hook_ms, 1),
                    "igni_steps_ms": answer.get("ms"),
                    "first_db_after_ms": round(first, 1),
                    "warm_db_ms": round(warm, 1),
                }
            )
        keys = ("held_before", "held_paused", "hook_ms", "igni_steps_ms", "first_db_after_ms")
        return {
            "rounds": out,
            "p50": {k: statistics.median(r[k] for r in out) for k in (*keys, "warm_db_ms")},
        }


class ResumeProbe:
    def __init__(
        self, *, pid: int, url: str, restored: Path, simulate: bool, connect_after_ms: float
    ) -> None:
        self.connect_after = connect_after_ms / 1000
        self.pages = ProcessPages(pid)
        self.url = url
        self.restored = restored
        self.simulate = simulate

    async def first_answer(self) -> float:
        """Until a bound igni answers (it refuses while unbound)."""
        started = time.perf_counter()
        async with connect(self.url, max_size=None) as ws:
            await ws.recv()
            window = Window(ws)
            while (await window.rpc("get_status")).get("error"):
                await asyncio.sleep(0.01)
        return time.perf_counter() - started

    async def session(self) -> None:
        """What a person does before igni goes idle: a turn, a few reads."""
        async with connect(self.url, max_size=None) as ws:
            await ws.recv()
            window = Window(ws)
            await window.rpc("get_session_id")
            await window.turn("say hi")
            await window.rpc("list_sessions")
            await window.rpc("get_status")

    async def idle(self, seconds: float) -> dict[str, Any]:
        """What igni touches and changes while nobody uses it."""
        before = await asyncio.to_thread(self.pages.contents)
        self.pages.clear(ProcessPages.CLEAR_SOFT_DIRTY)
        self.pages.clear(ProcessPages.CLEAR_REFERENCED)
        cpu = self.pages.cpu_seconds()
        await asyncio.sleep(seconds)
        cpu_ms = round((self.pages.cpu_seconds() - cpu) * 1000, 1)
        touched = self.pages.referenced_kb()
        touched_by = self.pages.referenced_by_mapping()
        soft_dirty, soft_by = self.pages.soft_dirty()
        after = await asyncio.to_thread(self.pages.contents)
        changed, changed_by = ProcessPages.changed(before, after)
        return {
            "seconds": seconds,
            "cpu_ms": cpu_ms,
            "changed_kb": changed * ProcessPages.PAGE // 1024,
            "changed_kb_by_mapping": changed_by,
            "touched_kb": touched,
            "touched_kb_by_mapping": touched_by,
            # Zero where the kernel has no soft-dirty bits: see ``changed_kb``.
            "soft_dirty_kb": soft_dirty * ProcessPages.PAGE // 1024,
            "soft_dirty_kb_by_mapping": soft_by,
        }

    def _next_generation(self) -> int:
        try:
            return int(json.loads(self.restored.read_text())["generation"]) + 1
        except (OSError, ValueError, KeyError):
            return 1

    async def resume(self) -> dict[str, Any]:
        pid = self.pages.pid
        os.kill(pid, signal.SIGSTOP)
        self.pages.clear(ProcessPages.CLEAR_REFERENCED)
        await asyncio.sleep(0.2)
        os.kill(pid, signal.SIGCONT)
        cpu = self.pages.cpu_seconds()
        staged = self.restored.with_name(".restored.probe")
        staged.write_text(
            json.dumps(
                {
                    "generation": self._next_generation(),
                    "at_ms": int(time.time() * 1000),
                    "clock_step_ms": 200.0,
                }
            )
        )
        t0 = time.perf_counter()
        os.replace(staged, self.restored)
        await asyncio.sleep(self.connect_after)
        async with connect(self.url, max_size=None) as ws:
            await ws.recv()
            handshake = time.perf_counter()
            window = Window(ws)
            await window.rpc("get_session_id")
            first_rpc = time.perf_counter()
            await window.rpc("list_sessions")
            first_db = time.perf_counter()
        referenced = self.pages.referenced_kb()
        by_mapping = self.pages.referenced_by_mapping()
        return {
            "handshake_ms": round((handshake - t0) * 1000, 1),
            "first_rpc_ms": round((first_rpc - t0) * 1000, 1),
            "first_db_rpc_ms": round((first_db - t0) * 1000, 1),
            "referenced_kb": referenced,
            "cpu_ms": round((self.pages.cpu_seconds() - cpu) * 1000, 1),
            "referenced_kb_by_mapping": by_mapping,
        }


class Summary:
    @staticmethod
    def of(resumes: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {"n": len(resumes)}
        for key in ("handshake_ms", "first_rpc_ms", "first_db_rpc_ms", "referenced_kb", "cpu_ms"):
            values = sorted(r[key] for r in resumes)
            out[key] = {
                "p50": statistics.median(values),
                "max": values[-1],
            }
        return out

    @staticmethod
    def timelines(log: Path) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for line in log.read_text(errors="replace").splitlines():
            if "resume timeline" not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record.get("timeline"), dict):
                found.append(record["timeline"])
        return found


@click.command()
@click.option("--out", type=click.Path(path_type=Path), required=True)
@click.option("--database-url", default=None, help="A migrated database; SQLite by default")
@click.option("--idle", "idle_seconds", default=60.0, help="Seconds of idle to measure")
@click.option("--settle", default=3.0, help="Seconds between the session and the idle window")
@click.option("--resumes", default=10, help="Simulated restores")
@click.option("--model-url", default=None, help="A real model's OpenAI-compatible URL")
@click.option("--model-id", default=None)
@click.option(
    "--model-key-env", default=None, help="The variable holding its key (never the key itself)"
)
@click.option("--repo-url", default=None, help="A repository cloned as igni's project")
@click.option("--task", default=None, help="A task for real agent runs, after the session")
@click.option(
    "--chats", type=int, multiple=True, default=(1, 3), help="Chats at once, per agent run"
)
@click.option("--task-timeout", default=900.0)
@click.option(
    "--pause-rounds",
    default=0,
    help="Run the before-suspend hook this often: connections held, and the first query after",
)
@click.option(
    "--base-settle", default=2.0, help="Seconds after ready at which the warm base is taken"
)
@click.option(
    "--save-pages",
    type=click.Path(path_type=Path),
    default=None,
    help="Write the base, bound and after-a-run page checksums here",
)
@click.option(
    "--env",
    "igni_env",
    multiple=True,
    help="KEY=VALUE for igni's process (MALLOC_ARENA_MAX=2, LD_PRELOAD=...)",
)
@click.option(
    "--connect-after-ms",
    default=30.0,
    help="From restored to the client connecting: the node agent's reply and the stream",
)
@click.option("--port", default=18765)
@click.option("--pid", type=int, default=None, help="Measure this igni instead of starting one")
@click.option("--ws", "ws_url", default=None, help="Its WebSocket URL, with --pid")
@click.option("--restored", type=click.Path(path_type=Path), default=None)
@click.option("--simulate/--no-simulate", default=True)
def main(
    out: Path,
    database_url: str | None,
    idle_seconds: float,
    settle: float,
    resumes: int,
    model_url: str | None,
    model_id: str | None,
    model_key_env: str | None,
    repo_url: str | None,
    task: str | None,
    chats: tuple[int, ...],
    task_timeout: float,
    pause_rounds: int,
    base_settle: float,
    save_pages: Path | None,
    igni_env: tuple[str, ...],
    connect_after_ms: float,
    port: int,
    pid: int | None,
    ws_url: str | None,
    restored: Path | None,
    simulate: bool,
) -> None:
    async def run() -> dict[str, Any]:
        igni: Igni | None = None
        result: dict[str, Any] = {}
        if pid is None:
            server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            igni = Igni(
                database_url=database_url,
                port=port,
                env=dict(item.split("=", 1) for item in igni_env),
            )
            model = ModelChoice(
                fake_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
                url=model_url,
                model_id=model_id,
                key_env=model_key_env,
                repo_url=repo_url,
            )
            igni.start(model)
            igni.wait_ready()
            assert igni.process is not None
            probe = ResumeProbe(
                pid=igni.process.pid,
                url=f"ws://127.0.0.1:{port}",
                restored=igni.run_dir / "restored",
                simulate=True,
                connect_after_ms=connect_after_ms,
            )
            # The warm base: what a snapshot taken now would hold.
            await asyncio.sleep(base_settle)
            base = await asyncio.to_thread(probe.pages.contents)
            result["rss_at_ready_kb"] = probe.pages.rss_kb()
            igni.hand_over()
            result["bind_to_first_answer_ms"] = round(await probe.first_answer() * 1000, 1)
            await asyncio.sleep(settle)
            bound = await asyncio.to_thread(probe.pages.contents)
            result["bound"] = ProcessPages.compare(base, bound) | {"rss_kb": probe.pages.rss_kb()}
            await probe.session()
            await asyncio.sleep(settle)
            ran = await asyncio.to_thread(probe.pages.contents)
            result["after_a_run"] = ProcessPages.compare(base, ran) | {
                "rss_kb": probe.pages.rss_kb()
            }
            if pause_rounds and database_url:
                pause = PauseProbe(
                    url=f"ws://127.0.0.1:{port}", run_dir=igni.run_dir, database_url=database_url
                )
                result["pause"] = await pause.run(pause_rounds)
            if task:
                agent = AgentRunProbe(pid=igni.process.pid, url=f"ws://127.0.0.1:{port}")
                result["agent_runs"] = []
                for count in chats:
                    result["agent_runs"].append(await agent.run(task, count, task_timeout))
                    await asyncio.sleep(settle)
                after = await asyncio.to_thread(probe.pages.contents)
                result["after_agent_runs"] = ProcessPages.compare(base, after) | {
                    "rss_kb": probe.pages.rss_kb()
                }
            if save_pages is not None:
                save_pages.mkdir(parents=True, exist_ok=True)
                for name, pages in (("base", base), ("bound", bound), ("ran", ran)):
                    ProcessPages.save(pages, save_pages / f"{name}.pages")
        else:
            assert ws_url and restored, "--ws and --restored with --pid"
            probe = ResumeProbe(
                pid=pid,
                url=ws_url,
                restored=restored,
                simulate=simulate,
                connect_after_ms=connect_after_ms,
            )
        try:
            if pid is not None:
                await probe.session()
                await asyncio.sleep(settle)
            if idle_seconds:
                result["idle"] = await probe.idle(idle_seconds)
            if probe.simulate and resumes:
                runs = []
                for _ in range(resumes):
                    runs.append(await probe.resume())
                    await asyncio.sleep(2.5)  # past the deferred rescan
                result["resumes"] = runs
                result["resume_summary"] = Summary.of(runs)
                if igni is not None:
                    result["igni_timelines"] = Summary.timelines(igni.log)
        finally:
            if igni is not None:
                igni.stop()
        return result

    result = asyncio.run(run())
    out.write_text(json.dumps(result, indent=1))
    brief = {k: v for k, v in result.items() if k not in ("resumes", "igni_timelines")}
    print(json.dumps(brief, indent=1))


if __name__ == "__main__":
    main()
