"""The failure patterns match what the shell tools actually emit — SWP-32 / SWP-54.

``ToolResultErrorDetector`` decides ``is_error`` for every tool result
that Agno did not raise on. The web client paints a tool card red off
that flag and the TUI draws its ✓ / ✗ from it, so a pattern that no
longer matches its emitter turns a failed command into a green tick and
the agent reads its own failure as a success.

Two patterns had drifted from their emitters:

* SWP-32 — foreground ``run_shell_command`` emits ``"Command exited
  with code N:"`` (``ProcessSupervisor.run_foregrounded``); the
  detector was still matching ``"[Exited with code N"`` only, which
  comes from ``await_process``. Every non-zero exit of the product's
  highest-traffic tool rendered as success.
* SWP-54 — the background startup-window crash emits ``"Background
  process exited with code N:"``; the detector matched ``"Background
  process exited immediately (code N)"``, a wording nothing emits.

The strings below are copied verbatim from the emitters, and the last
class pins them against the emitters' own source so the next rewording
breaks a test rather than a user's feedback loop.
"""

from pathlib import Path

import pytest

from ember_code.core.tools import process_supervisor, shell
from ember_code.protocol.tool_error_conventions import ToolResultErrorDetector


@pytest.fixture
def detector():
    return ToolResultErrorDetector.default()


class TestFailuresAreFlagged:
    """Exactly the strings the shell tools produce on a non-zero exit."""

    @pytest.mark.parametrize(
        "result",
        [
            # run_foregrounded — SWP-32.
            "Command exited with code 1:\nbash: nope: command not found\n",
            "Command exited with code 127:\n",
            "Command exited with code 2:\npytest collected 3 errors",
            # run_backgrounded, crashed inside the 3 s window — SWP-54.
            "Background process exited with code 1:\nAddress already in use\n",
            "Background process exited with code 137:\n",
            # await_process / watch_process.
            "[Exited with code 1 after 4s — failed]\nOutput:\nboom",
            "[Exited with code 1 after 12s]\nNo new output before exit.",
            # The Error: prefix convention, unchanged.
            "Error: File not found",
        ],
    )
    def test_flagged(self, detector, result):
        assert detector.is_error(result) is True


class TestSuccessesAreNotFlagged:
    """Code 0 and the running/plain shapes must stay green."""

    @pytest.mark.parametrize(
        "result",
        [
            "Command exited with code 0:\n",
            "Background process exited with code 0:\nstarted\n",
            # The deliberate "this was a clean fast run" wording.
            "Background process finished during startup window (code 0):\nok\n",
            "[Exited with code 0 after 4s — succeeded]\nOutput:\nall tests passed",
            "Background process running (PID 4242): npm run dev\n",
            "Command still running after 30s — backgrounded as PID 4242.\n",
            "hello world\n",
            "",
            # A Read / grep whose *body* contains the conventions — the
            # left-anchoring is what keeps these from misfiring.
            "log.txt:41: Command exited with code 1:\n",
            "The build log says Error: nope",
        ],
    )
    def test_not_flagged(self, detector, result):
        assert detector.is_error(result) is False


class TestPatternsStayPinnedToTheirEmitters:
    """Guard the drift itself, not just today's strings.

    The detector and the emitters are in different packages with no
    shared constant, so nothing but this test stops a reworded f-string
    from silently unhooking the flag again.
    """

    @pytest.mark.parametrize(
        "module, literal",
        [
            (process_supervisor, 'f"Command exited with code {rc}:'),
            (process_supervisor, 'f"Background process exited with code {rc}:'),
            (shell, 'f"[Exited with code {rc} after {elapsed:.0f}s'),
        ],
    )
    def test_emitter_literal_unchanged(self, module, literal):
        source = Path(module.__file__).read_text()
        assert literal in source, (
            f"{Path(module.__file__).name} no longer emits {literal!r}; "
            "update the matching pattern in protocol/tool_error_conventions.py "
            "in the same commit or failed tool calls will render as successes."
        )
