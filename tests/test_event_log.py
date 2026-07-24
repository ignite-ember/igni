"""Session event-log unit tests.

Direct tests for :meth:`Session.append_event` and the
``get_chat_history`` splicing of ``role:"visualization"`` turns
from the event log. Real-DB round-trip tests live in
:file:`test_session_data_real_db.py` — this file uses
``Session.__new__`` (bypassing ``__init__``) so the reducer logic
can be exercised without spinning up the full Agno stack.
"""

from __future__ import annotations

import json as _json
from unittest.mock import AsyncMock, MagicMock

from ember_code.core.session.core import Session


def _bare_session() -> Session:
    """Build a Session without running ``__init__`` — we only
    need the event-log attributes for these tests."""
    sess = Session.__new__(Session)
    sess.event_log = []
    sess._event_seq = 0
    sess.persistence = None
    return sess


class TestAppendEvent:
    async def test_seq_monotonic(self):
        sess = _bare_session()
        await sess.append_event("visualization_delta", {"a": 1}, run_id="r1")
        await sess.append_event("visualization_delta", {"a": 2}, run_id="r1")
        await sess.append_event("visualization_delta", {"a": 3}, run_id="r2")

        seqs = [e.seq for e in sess.event_log]
        assert seqs == [1, 2, 3]

    async def test_payload_is_copied_not_referenced(self):
        # Mutating the caller's payload after append must not
        # bleed into the stored entry. Guarantees replay sees
        # exactly what was live at emission time.
        sess = _bare_session()
        payload = {"json": "abc"}
        await sess.append_event("visualization_delta", payload, run_id="r1")
        payload["json"] = "MUTATED"
        assert sess.event_log[0].payload["json"] == "abc"

    async def test_carries_run_id_and_type(self):
        sess = _bare_session()
        await sess.append_event("visualization_delta", {"x": 1}, run_id="run-42")
        entry = sess.event_log[0]
        assert entry.run_id == "run-42"
        assert entry.type == "visualization_delta"
        assert entry.payload == {"x": 1}

    async def test_calls_persistence_save_event_log(self):
        # Every append triggers persistence — the log is small and
        # ordering matters for replay, so we skip batching. Verified
        # via a spy on ``save_event_log``.
        sess = _bare_session()
        sess.persistence = MagicMock()
        sess.persistence.save_event_log = AsyncMock()

        await sess.append_event("visualization_delta", {"x": 1}, run_id="r1")
        await sess.append_event("visualization_delta", {"x": 2}, run_id="r1")

        assert sess.persistence.save_event_log.await_count == 2
        # Last call sees BOTH entries — atomic-replace semantics.
        last_call = sess.persistence.save_event_log.await_args
        args = last_call.args if last_call else ()
        assert args and len(args[0]) == 2

    async def test_persistence_failure_does_not_swallow_in_memory(self):
        # A DB write failure must not lose the in-memory entry —
        # live listeners already got the event and reload-recovery
        # is the only sacrifice.
        sess = _bare_session()
        sess.persistence = MagicMock()
        sess.persistence.save_event_log = AsyncMock(side_effect=RuntimeError("db down"))

        await sess.append_event("visualization_delta", {"x": 1}, run_id="r1")

        assert len(sess.event_log) == 1
        assert sess.event_log[0].payload == {"x": 1}


class TestGetChatHistoryVizSplicing:
    """The BE-side splicing of viz cards from the event log into
    the chat history — replaces the reverted
    ``session.visualizations`` splicer with an event-log-driven
    one. Structural tests only; the real ``get_chat_history`` path
    is exercised elsewhere with real Agno sessions.
    """

    def _splice(self, history: list[dict], event_log: list[dict]) -> list[dict]:
        """Extracted copy of the splicing loop from
        ``BackendServer.get_chat_history`` so we can test the
        pairing logic without wiring up a full Session/DB."""
        out = list(history)
        viz_events = [
            e for e in event_log if isinstance(e, dict) and e.get("type") == "visualization_delta"
        ]
        if not viz_events:
            return out

        SPAWN_TOOLS = {"spawn_agent", "spawn_team"}
        spawn_indices_by_run: dict[str, list[int]] = {}
        last_by_run: dict[str, int] = {}
        for i, t in enumerate(out):
            rid = str(t.get("run_id") or "")
            if rid:
                last_by_run[rid] = i
            if t.get("role") == "tool" and t.get("tool_name") in SPAWN_TOOLS and rid:
                spawn_indices_by_run.setdefault(rid, []).append(i)

        by_run: dict[str, list[dict]] = {}
        for ev in viz_events:
            rid = str(ev.get("run_id") or "")
            by_run.setdefault(rid, []).append(ev)
        for group in by_run.values():
            group.sort(key=lambda e: int(e.get("seq", 0) or 0))

        insertions: dict[int, list[dict]] = {}
        for rid, group in by_run.items():
            spawn_idxs = spawn_indices_by_run.get(rid, [])
            for n, ev in enumerate(group):
                payload = ev.get("payload") or {}
                json_str = str(payload.get("json") or "")
                if not json_str:
                    continue
                try:
                    spec = _json.loads(json_str)
                except Exception:
                    continue
                if not isinstance(spec, dict):
                    continue
                turn_out = {
                    "role": "visualization",
                    "spec_id": str(payload.get("spec_id") or ""),
                    "spec": spec,
                    "source_agent": "visualizer",
                    "run_id": rid,
                    "seq": int(ev.get("seq", 0) or 0),
                }
                if n < len(spawn_idxs):
                    target = spawn_idxs[n]
                elif rid in last_by_run:
                    target = last_by_run[rid]
                else:
                    target = -1
                insertions.setdefault(target, []).append(turn_out)

        for target in sorted(insertions.keys(), reverse=True):
            group = sorted(insertions[target], key=lambda t: t["seq"])
            if target < 0:
                out.extend(group)
            else:
                out[target + 1 : target + 1] = group

        return out

    def test_viz_inserted_after_matching_spawn_agent_tool_turn(self):
        history = [
            {"role": "user", "run_id": "r1", "content": "hi"},
            {"role": "tool", "tool_name": "spawn_agent", "run_id": "r1"},
            {"role": "assistant", "run_id": "r1", "content": "here"},
        ]
        event_log = [
            {
                "seq": 1,
                "run_id": "r1",
                "timestamp_ms": 1,
                "type": "visualization_delta",
                "payload": {"spec_id": "s1", "json": '{"root":"r"}'},
            }
        ]
        result = self._splice(history, event_log)
        # Viz card lands right AFTER the spawn_agent tool turn.
        assert [t["role"] for t in result] == [
            "user",
            "tool",
            "visualization",
            "assistant",
        ]
        assert result[2]["spec_id"] == "s1"
        assert result[2]["spec"] == {"root": "r"}

    def test_nth_viz_pairs_with_nth_spawn_within_run(self):
        # Three spawn calls, three viz events — each viz should
        # land right after its own spawn. This is the "intermediate
        # messages between charts" bug the reverted patchwork
        # regressed on.
        history = [
            {"role": "user", "run_id": "r1"},
            {"role": "tool", "tool_name": "spawn_agent", "run_id": "r1"},
            {"role": "tool", "tool_name": "spawn_agent", "run_id": "r1"},
            {"role": "tool", "tool_name": "spawn_agent", "run_id": "r1"},
            {"role": "assistant", "run_id": "r1"},
        ]
        event_log = [
            {
                "seq": i,
                "run_id": "r1",
                "timestamp_ms": i,
                "type": "visualization_delta",
                "payload": {"spec_id": f"s{i}", "json": '{"root":"r"}'},
            }
            for i in (1, 2, 3)
        ]
        result = self._splice(history, event_log)
        # Expect the shape: user, spawn, viz, spawn, viz, spawn, viz, assistant.
        assert [t["role"] for t in result] == [
            "user",
            "tool",
            "visualization",
            "tool",
            "visualization",
            "tool",
            "visualization",
            "assistant",
        ]
        # Pairing order preserved: viz #1 after spawn #1, etc.
        vis = [t for t in result if t["role"] == "visualization"]
        assert [v["spec_id"] for v in vis] == ["s1", "s2", "s3"]

    def test_viz_falls_back_to_last_turn_when_no_matching_spawn(self):
        # Historical run where the tool turn isn't reachable (e.g.
        # a synthetic run reconstructed from the summary). The card
        # still lands within its run, just at the tail.
        history = [
            {"role": "user", "run_id": "r1"},
            {"role": "assistant", "run_id": "r1"},
        ]
        event_log = [
            {
                "seq": 1,
                "run_id": "r1",
                "timestamp_ms": 1,
                "type": "visualization_delta",
                "payload": {"spec_id": "s1", "json": '{"root":"r"}'},
            }
        ]
        result = self._splice(history, event_log)
        # Viz appears after the last turn of the run.
        assert [t["role"] for t in result] == ["user", "assistant", "visualization"]

    def test_viz_with_unknown_run_id_appends_at_tail(self):
        # Belt-and-suspenders: an orphaned viz (run_id not in
        # history) doesn't disappear — appended at the very end
        # so the user still sees it after a session restore.
        history = [
            {"role": "user", "run_id": "r1"},
        ]
        event_log = [
            {
                "seq": 1,
                "run_id": "ORPHAN",
                "timestamp_ms": 1,
                "type": "visualization_delta",
                "payload": {"spec_id": "s1", "json": '{"root":"r"}'},
            }
        ]
        result = self._splice(history, event_log)
        assert result[-1]["role"] == "visualization"

    def test_malformed_json_payload_is_skipped(self):
        # A corrupt entry (truncated JSON) doesn't crash the
        # splicer — we drop it and keep going with the rest.
        history = [
            {"role": "user", "run_id": "r1"},
            {"role": "tool", "tool_name": "spawn_agent", "run_id": "r1"},
        ]
        event_log = [
            {
                "seq": 1,
                "run_id": "r1",
                "timestamp_ms": 1,
                "type": "visualization_delta",
                "payload": {"spec_id": "bad", "json": '{"root": "r"'},
            },  # truncated
            {
                "seq": 2,
                "run_id": "r1",
                "timestamp_ms": 2,
                "type": "visualization_delta",
                "payload": {"spec_id": "good", "json": '{"root":"r"}'},
            },
        ]
        result = self._splice(history, event_log)
        vis = [t for t in result if t["role"] == "visualization"]
        assert len(vis) == 1
        assert vis[0]["spec_id"] == "good"

    def test_non_viz_events_ignored(self):
        # The event log stores whatever the BE emits — future
        # entries (content_preview, orchestrate_event, etc.) MUST
        # NOT accidentally get spliced as viz cards.
        history = [
            {"role": "user", "run_id": "r1"},
            {"role": "tool", "tool_name": "spawn_agent", "run_id": "r1"},
        ]
        event_log = [
            {
                "seq": 1,
                "run_id": "r1",
                "timestamp_ms": 1,
                "type": "content_preview",
                "payload": {"text": "hi"},
            },
            {
                "seq": 2,
                "run_id": "r1",
                "timestamp_ms": 2,
                "type": "orchestrate_event",
                "payload": {},
            },
        ]
        result = self._splice(history, event_log)
        assert all(t["role"] != "visualization" for t in result)
