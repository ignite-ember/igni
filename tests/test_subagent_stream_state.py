"""Tests for :class:`SubAgentStreamState` — the Pydantic model that
replaces the 11 nonlocals in ``_run_agent_streaming``.

The state model is a pure data holder; there's no behaviour to test
beyond construction, defaults, and mutability semantics. These tests
lock in the invariants that ``_run_agent_streaming`` relies on so a
future refactor that changes defaults can't silently regress the
streaming logic.
"""

from __future__ import annotations

from ember_code.core.tools.subagent_stream import SubAgentStreamState


class TestDefaults:
    def test_required_fields_are_only_identity(self):
        # Constructing with just the identity fields must succeed.
        # If any other field becomes required, the caller in
        # ``_run_agent_streaming`` breaks — this test catches that.
        s = SubAgentStreamState(agent_path_id="root")
        assert s.agent_path_id == "root"
        assert s.card_id == ""

    def test_log_starts_empty(self):
        s = SubAgentStreamState(agent_path_id="root")
        assert s.log == []

    def test_content_buffers_start_empty(self):
        s = SubAgentStreamState(agent_path_id="root")
        assert s.content_buf == ""
        assert s.last_preview == ""
        assert s.current_tool is None
        assert s.last_update == 0.0

    def test_visualizer_state_starts_at_zero(self):
        s = SubAgentStreamState(agent_path_id="root")
        assert s.vis_last_emitted_len == 0
        assert s.vis_last_emit_at == 0.0

    def test_run_identity_starts_unset(self):
        s = SubAgentStreamState(agent_path_id="root")
        assert s.current_run_id is None
        assert s.current_session_id is None
        assert s.parent_top_run_id is None

    def test_completion_tracking_starts_false(self):
        s = SubAgentStreamState(agent_path_id="root")
        assert s.agent_completed_emitted is False
        assert s.completed_content == ""


class TestVizSpecId:
    def test_default_is_generated(self):
        # Fresh hex UUID prefix each construction — every visualizer
        # invocation must get its own spec_id so the FE dedup works.
        s = SubAgentStreamState(agent_path_id="root")
        assert isinstance(s.vis_spec_id, str)
        assert len(s.vis_spec_id) == 12  # hex prefix length

    def test_distinct_instances_get_distinct_ids(self):
        # Regression: if this generator became a class-level default
        # instead of a factory, every stream would share the same
        # spec_id and viz cards would collide.
        a = SubAgentStreamState(agent_path_id="root")
        b = SubAgentStreamState(agent_path_id="root")
        assert a.vis_spec_id != b.vis_spec_id

    def test_explicit_spec_id_wins(self):
        # Callers should be able to supply a fixed spec_id — e.g.
        # for a resume path that must keep the same card id.
        s = SubAgentStreamState(agent_path_id="root", vis_spec_id="fixed-123")
        assert s.vis_spec_id == "fixed-123"


class TestMutability:
    def test_run_id_can_be_latched_after_construction(self):
        # The handler mutates ``current_run_id`` when the first event
        # carrying a run_id arrives. Mutation must succeed —
        # ``ConfigDict`` doesn't freeze the model.
        s = SubAgentStreamState(agent_path_id="root")
        s.current_run_id = "run-42"
        assert s.current_run_id == "run-42"

    def test_log_append_persists(self):
        s = SubAgentStreamState(agent_path_id="root")
        s.log.append("  │  ├─ read_file(path=…)")
        s.log.append("  │  │  └─ ok")
        assert len(s.log) == 2
        assert s.log[-1].endswith("ok")

    def test_content_buf_grows(self):
        s = SubAgentStreamState(agent_path_id="root")
        s.content_buf += "hello "
        s.content_buf += "world"
        assert s.content_buf == "hello world"

    def test_agent_completed_flag_flips_once(self):
        s = SubAgentStreamState(agent_path_id="root")
        s.agent_completed_emitted = True
        # Sanity: the guard the belt-and-suspenders post-loop emit
        # uses works — once set, subsequent checks see it True.
        assert s.agent_completed_emitted is True


class TestIdentityFields:
    def test_agent_path_id_root(self):
        s = SubAgentStreamState(agent_path_id="root")
        assert s.agent_path_id == "root"

    def test_agent_path_id_nested(self):
        s = SubAgentStreamState(agent_path_id="architect.editor")
        assert s.agent_path_id == "architect.editor"

    def test_card_id_stamped_on_state_used_by_emit(self):
        # ``_emit`` reads ``card_id`` off the state to stamp every
        # progress event. Constructing with a card_id must preserve
        # it exactly.
        s = SubAgentStreamState(agent_path_id="root", card_id="card-abc")
        assert s.card_id == "card-abc"
