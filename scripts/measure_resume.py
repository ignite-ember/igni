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


class Igni:
    """An igni backend started here, in the cloud profile, as a warm base starts."""

    def __init__(self, *, database_url: str | None, port: int) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="resume-"))
        self.run_dir = self.tmp / "run"
        self.project = self.tmp / "project"
        for path in (self.run_dir, self.project, self.tmp / "home"):
            path.mkdir()
        self.database_url = database_url or f"sqlite:///{self.tmp / 'shared.db'}"
        self.port = port
        self.log = self.tmp / "igni.log"
        self.process: subprocess.Popen[bytes] | None = None

    def start(self, model_url: str) -> None:
        (self.run_dir / "managed-settings.yaml").write_text(
            json.dumps(
                {
                    "models": {
                        "default": "fake",
                        "registry": {
                            "fake": {
                                "provider": "openai_like",
                                "model_id": "fake",
                                "url": model_url,
                                "api_key": "x",
                                "context_window": 32000,
                            }
                        },
                    }
                }
            )
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("IGNI_") or k in ("IGNI_RESUME_PAGES",)
        }
        env.update(
            IGNI_PROFILE="cloud", IGNI_RUN_DIR=str(self.run_dir), HOME=str(self.tmp / "home")
        )
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
            igni = Igni(database_url=database_url, port=port)
            igni.start(f"http://127.0.0.1:{server.server_address[1]}/v1")
            igni.wait_ready()
            assert igni.process is not None
            probe = ResumeProbe(
                pid=igni.process.pid,
                url=f"ws://127.0.0.1:{port}",
                restored=igni.run_dir / "restored",
                simulate=True,
                connect_after_ms=connect_after_ms,
            )
            result["rss_at_ready_kb"] = probe.pages.referenced_kb()
            igni.hand_over()
            result["bind_to_first_answer_ms"] = round(await probe.first_answer() * 1000, 1)
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
            await probe.session()
            await asyncio.sleep(settle)
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
