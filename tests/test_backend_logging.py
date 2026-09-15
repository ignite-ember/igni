"""The backend keeps a log without being asked to.

It did not, and that is the single reason a string of failures each
cost an afternoon: ``--debug`` is unreachable (the desktop app builds
the argv), and the env var that replaced it had to be set by someone
who already suspected a problem and knew its name. The shipped product
wrote no record of anything.
"""

from __future__ import annotations

import logging

from ember_code.backend import logging_setup


def _detach() -> None:
    pkg = logging.getLogger("ember_code")
    for h in list(pkg.handlers):
        if getattr(h, logging_setup._HANDLER_MARK, False):
            h.close()
            pkg.removeHandler(h)


def test_a_log_is_written_with_nothing_set(tmp_path, monkeypatch):
    """No flag, no env var. This is the whole point."""
    monkeypatch.delenv("IGNI_DEBUG_LOG", raising=False)
    path = tmp_path / "logs" / "backend.log"
    try:
        assert logging_setup.configure_logging(path=path) == path
        logging.getLogger("ember_code.something").info("hello from the backend")
        assert "hello from the backend" in path.read_text()
    finally:
        _detach()


def test_the_env_var_now_chooses_the_level_not_whether_to_log(tmp_path, monkeypatch):
    """``IGNI_DEBUG_LOG`` was an on-switch. It is a dial now, which is
    the question people meant to ask when they set it."""
    monkeypatch.delenv("IGNI_DEBUG_LOG", raising=False)
    assert logging_setup.resolve_level() == logging.INFO

    monkeypatch.setenv("IGNI_DEBUG_LOG", "1")
    assert logging_setup.resolve_level() == logging.DEBUG
    monkeypatch.delenv("IGNI_DEBUG_LOG")
    assert logging_setup.resolve_level(debug_flag=True) == logging.DEBUG


def test_debug_records_are_kept_out_of_the_default_log(tmp_path, monkeypatch):
    monkeypatch.delenv("IGNI_DEBUG_LOG", raising=False)
    path = tmp_path / "backend.log"
    try:
        logging_setup.configure_logging(path=path)
        log = logging.getLogger("ember_code.noisy")
        log.debug("chatter")
        log.warning("something worth keeping")
        text = path.read_text()
        assert "chatter" not in text
        assert "something worth keeping" in text
    finally:
        _detach()


def test_configuring_twice_does_not_double_every_line(tmp_path, monkeypatch):
    """Two handlers means two copies of every record, which makes a log
    read like a stutter and doubles its size on disk."""
    monkeypatch.delenv("IGNI_DEBUG_LOG", raising=False)
    path = tmp_path / "backend.log"
    try:
        logging_setup.configure_logging(path=path)
        logging_setup.configure_logging(path=path)
        logging.getLogger("ember_code.once").warning("only once")
        assert path.read_text().count("only once") == 1
    finally:
        _detach()


def test_an_unwritable_location_does_not_take_the_backend_down(tmp_path, monkeypatch):
    """A backend with no log has a diagnosis problem. A backend that
    exits because it has no log has a much larger one, and read-only or
    full home directories are real."""
    monkeypatch.delenv("IGNI_DEBUG_LOG", raising=False)
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory")
    try:
        assert logging_setup.configure_logging(path=blocked / "nested" / "backend.log") is None
    finally:
        _detach()


def test_the_path_can_be_pointed_somewhere_specific(tmp_path, monkeypatch):
    """Support requests and tests both want to choose the location."""
    monkeypatch.setenv("IGNI_DEBUG_LOG_PATH", str(tmp_path / "elsewhere.log"))

    assert logging_setup.resolve_log_path() == tmp_path / "elsewhere.log"


def test_the_default_location_is_under_the_ember_home(monkeypatch):
    monkeypatch.delenv("IGNI_DEBUG_LOG_PATH", raising=False)

    path = logging_setup.resolve_log_path()

    assert path.parent.name == "logs"
    assert path.name == "backend.log"


def test_the_log_rotates(tmp_path, monkeypatch):
    """Always-on means unbounded, unless something bounds it."""
    monkeypatch.delenv("IGNI_DEBUG_LOG", raising=False)
    path = tmp_path / "backend.log"
    try:
        logging_setup.configure_logging(path=path)
        handler = next(
            h
            for h in logging.getLogger("ember_code").handlers
            if getattr(h, logging_setup._HANDLER_MARK, False)
        )
        assert handler.maxBytes > 0
        assert handler.backupCount > 0
    finally:
        _detach()
