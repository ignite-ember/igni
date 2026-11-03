"""The ``igni`` prompt sends every line to exactly one place, and always says goodbye.

:class:`InteractiveSessionLoop` reads a line and offers it to a chain of
handlers — bare quit word, slash command, ``/skill``, model turn — where
the first taker wins. What is pinned here:

* a blank line runs nothing; ``quit``/``exit`` (any case), Ctrl+C and EOF
  end the loop, and ``SessionEnd`` fires on every one of those ends — and
  also when ``/quit`` leaves by ``SystemExit``;
* a built-in slash command is not a model turn; a skill or a markdown
  command runs its rendered prompt as one (it used to be printed as
  information and never run — the bug this file found); a slash command
  nobody knows goes to the model as a message;
* anything else is a model turn;
* the banner names an available update, stays quiet when the check
  failed, and says ``(resumed)`` for a resumed session;
* ephemeral agents are cleaned up at the end when the settings say so.

Only the session around the loop is a double (as in
``test_a_failed_headless_run_exits_non_zero.py``); the loop, its handler
chain, the real slash-command dispatcher, the real :class:`SkillPool`,
markdown-command discovery and the real :class:`HookExecutor` run as shipped.
Two seams are patched: the terminal prompt and the network update check.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ember_code.core.config.settings import Settings
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.session.interactive_loop import InteractiveSessionLoop
from ember_code.core.skills.loader import SkillPool
from ember_code.core.utils.update_checker_schemas import UpdateError, UpdateInfo

LOOP = "ember_code.core.session.interactive_loop"


class _Pool:
    def __init__(self, ephemeral: int = 0) -> None:
        self._ephemeral = ephemeral
        self.agent_names = ["editor", "reviewer"]
        self.cleaned = False

    def cleanup_ephemeral(self) -> int:
        self.cleaned = True
        return self._ephemeral


class _Session:
    """What the loop reaches on a :class:`Session`, and nothing more."""

    def __init__(self, tmp_path: Path, ephemeral: int = 0) -> None:
        self.session_id = "abc12345"
        self.settings = Settings()
        self.project_dir = tmp_path
        self.display = MagicMock()
        self.pool = _Pool(ephemeral)
        self.skill_pool = SkillPool()
        self.hooks_map: dict[str, list[Any]] = {"PreToolUse": [object(), object()]}
        self.hook_executor = HookExecutor({})
        self.messages: list[str] = []
        self.last_turn_failure: str | None = None

    def group_root(self, _kind: str) -> Path | None:
        return None

    async def handle_message(self, message: str) -> str:
        self.messages.append(message)
        return f"answered: {message}"


def _skill(root: Path, name: str, body: str) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n{body}\n", encoding="utf-8"
    )


class _Run:
    def __init__(self, loop: InteractiveSessionLoop, session: _Session, events: list[str]):
        self.loop = loop
        self.session = session
        self.events = events

    def infos(self) -> list[str]:
        return [c.args[0] for c in self.session.display.print_info.call_args_list]


@pytest.fixture
def make_loop(tmp_path: Path, monkeypatch) -> Iterator[Any]:
    def build(
        lines: list[Any],
        *,
        update: UpdateInfo | None = None,
        resume: str | None = None,
        ephemeral: int = 0,
    ) -> _Run:
        session = _Session(tmp_path, ephemeral)
        events: list[str] = []

        async def record(self: HookExecutor, event: str, payload: dict[str, Any]) -> Any:
            events.append(event)

        monkeypatch.setattr(HookExecutor, "execute", record)
        script = iter(lines)

        def ask(*_: Any, **__: Any) -> str:
            line = next(script)
            if isinstance(line, BaseException):
                raise line
            return line

        async def check() -> UpdateInfo:
            return update or UpdateInfo()

        monkeypatch.setattr(f"{LOOP}.Prompt.ask", ask)
        monkeypatch.setattr(f"{LOOP}.check_for_update", check)
        with patch("ember_code.core.session.session_run.Session", return_value=session):
            loop = InteractiveSessionLoop(Settings(), resume_session_id=resume)
        return _Run(loop, session, events)

    yield build


async def test_a_blank_line_runs_nothing_and_quit_says_goodbye(make_loop):
    run = make_loop(["   ", "QUIT"])

    await run.loop.run()

    assert run.session.messages == []
    assert "Goodbye!" in run.infos()
    assert run.events == ["SessionStart", "SessionEnd"]


@pytest.mark.parametrize("ending", [KeyboardInterrupt(), EOFError()])
async def test_ctrl_c_and_eof_at_the_prompt_end_the_session(make_loop, ending):
    run = make_loop([ending])

    await run.loop.run()

    assert run.events[-1] == "SessionEnd"


async def test_a_plain_line_is_a_model_turn(make_loop):
    run = make_loop(["fix the failing test", "exit"])

    await run.loop.run()

    assert run.session.messages == ["fix the failing test"]
    run.session.display.print_response.assert_called_once_with("answered: fix the failing test")


@pytest.mark.parametrize("ending", [KeyboardInterrupt(), EOFError()])
async def test_ctrl_c_or_eof_during_a_turn_ends_the_session(make_loop, ending):
    run = make_loop(["go"])

    async def interrupted(_message: str) -> str:
        raise ending

    run.session.handle_message = interrupted

    await run.loop.run()

    assert run.events == ["SessionStart", "SessionEnd"]


async def test_slash_quit_leaves_by_system_exit_and_still_ends_the_session(make_loop):
    run = make_loop(["/quit"])

    with pytest.raises(SystemExit):
        await run.loop.run()

    assert run.events == ["SessionStart", "SessionEnd"]
    assert run.session.messages == []


async def test_a_built_in_slash_command_is_not_a_model_turn(make_loop):
    run = make_loop(["/help schedule", "exit"])

    await run.loop.run()

    assert run.session.messages == []
    run.session.display.print_markdown.assert_called()


async def test_a_skill_command_runs_its_prompt_as_a_turn(make_loop, tmp_path):
    """Found writing this file: the dispatcher answered ``/<skill>`` with
    the skill's rendered prompt and the REPL *printed* it, as information,
    and ran nothing. The skill handler further down the chain was never
    reached, because the dispatcher matched the same skill first."""
    run = make_loop(["/deploy-now staging", "exit"])
    _skill(tmp_path / "skills", "deploy-now", "Deploy to $ARGUMENTS")
    run.session.skill_pool.load_directory(tmp_path / "skills")

    await run.loop.run()

    assert run.session.messages == ["Deploy to staging"]
    run.session.display.print_response.assert_called_once_with("answered: Deploy to staging")
    assert "Deploy to staging" not in run.infos()


async def test_a_markdown_command_runs_its_prompt_as_a_turn(make_loop, tmp_path):
    run = make_loop(["/summarise the README", "exit"])
    commands = tmp_path / ".igni" / "commands"
    commands.mkdir(parents=True)
    (commands / "summarise.md").write_text(
        "---\ndescription: summarise\n---\nSummarise $ARGUMENTS in one line.\n",
        encoding="utf-8",
    )

    await run.loop.run()

    assert [m.strip() for m in run.session.messages] == ["Summarise the README in one line."]


async def test_an_unknown_slash_command_goes_to_the_model_as_a_message(make_loop):
    run = make_loop(["/no-such-thing here", "exit"])

    await run.loop.run()

    assert run.session.messages == ["/no-such-thing here"]


async def test_the_banner_names_an_available_update_and_the_session(make_loop):
    update = UpdateInfo(available=True, current_version="1.0.0", latest_version="1.1.0")
    run = make_loop(["exit"], update=update)

    await run.loop.run()

    run.session.display.print_warning.assert_called_once_with(update.message)
    infos = run.infos()
    assert "Loaded agents: editor, reviewer" in infos
    assert "Loaded hooks: 2" in infos
    assert "Session: abc12345" in infos


async def test_a_failed_update_check_shows_no_warning(make_loop):
    run = make_loop(["exit"], update=UpdateInfo(error=UpdateError.NETWORK))

    await run.loop.run()

    run.session.display.print_warning.assert_not_called()


async def test_no_update_shows_no_warning(make_loop):
    run = make_loop(["exit"], update=UpdateInfo(available=False))

    await run.loop.run()

    run.session.display.print_warning.assert_not_called()


async def test_a_resumed_session_says_so(make_loop):
    run = make_loop(["exit"], resume="abc12345")

    await run.loop.run()

    assert "Session: abc12345 (resumed)" in run.infos()


async def test_ephemeral_agents_are_cleaned_up_at_the_end(make_loop):
    run = make_loop(["exit"], ephemeral=3)
    run.session.settings.orchestration.auto_cleanup = True

    await run.loop.run()

    assert run.session.pool.cleaned
    assert "Cleaned up 3 ephemeral agent(s)." in run.infos()


async def test_nothing_is_cleaned_up_when_the_settings_say_not_to(make_loop):
    run = make_loop(["exit"], ephemeral=3)
    run.session.settings.orchestration.auto_cleanup = False

    await run.loop.run()

    assert not run.session.pool.cleaned
