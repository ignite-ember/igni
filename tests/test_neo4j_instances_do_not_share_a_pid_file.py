"""Every Neo4j instance gets its own run directory.

The run directory holds the pid file, and was the one path left at its
default — $NEO4J_HOME/run, shared by every instance. The launcher reads
that pid file before starting, so whichever Neo4j started second refused
with "Neo4j is already running": a session could not hold a code index
and an artifact store at once, and two windows could not both have one.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.backend.neo4j_runtime import Neo4jRuntime


def _config(runtime: Neo4jRuntime, root: Path, name: str) -> dict[str, str]:
    data = root / name / "data"
    text = runtime._build_config(7687, 7474, "pw", data, root / name / "logs")
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))


def test_the_run_directory_is_set_explicitly(tmp_path):
    runtime = Neo4jRuntime(data_dir=tmp_path)

    assert "server.directories.run" in _config(runtime, tmp_path, "a")


def test_two_instances_never_share_a_run_directory(tmp_path):
    runtime = Neo4jRuntime(data_dir=tmp_path)

    first = _config(runtime, tmp_path, "project-a-knowledge")["server.directories.run"]
    second = _config(runtime, tmp_path, "project-b-knowledge")["server.directories.run"]

    assert first != second


def test_the_run_directory_is_absolute_and_exists(tmp_path):
    """Relative would resolve against NEO4J_HOME — the shared directory
    this exists to get away from."""
    runtime = Neo4jRuntime(data_dir=tmp_path)

    run = Path(_config(runtime, tmp_path, "a")["server.directories.run"])

    assert run.is_absolute() and run.is_dir()
