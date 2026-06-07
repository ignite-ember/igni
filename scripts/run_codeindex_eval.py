"""Run the CodeIndex eval suite end-to-end.

Loads ``evals/codeindex.yaml``, sets up a Session, runs every case
against the configured main agent, prints a summary, optionally
dumps per-case detail to JSON.

Flags:

- ``--no-codeindex``: skip the JSONL ``apply_delta`` step in the
  setup hook. The chroma dir for HEAD never gets populated, so
  Session's tool gate hides ``codeindex_query`` from the agent —
  every case has to fall back to shell / Read / Grep. Used by the
  comparison report.
- ``--out PATH``: dump per-case detail (timing, tool trace,
  response text, judge result) to a JSON file the comparison
  script can diff.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
# Quiet the noisier libraries.
for n in ('httpx', 'httpcore', 'urllib3', 'agno', 'chromadb', 'asyncio'):
    logging.getLogger(n).setLevel(logging.WARNING)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--no-codeindex',
        action='store_true',
        help='Skip codeindex JSONL apply — agent has no codeindex_query tool.',
    )
    parser.add_argument(
        '--out',
        type=Path,
        help='Dump per-case JSON detail to this path.',
    )
    parser.add_argument(
        '--suite',
        default='codeindex',
        help='Eval suite YAML name (without ``.yaml``). Default: codeindex.',
    )
    parser.add_argument(
        '--target-project-dir',
        type=Path,
        help=(
            'Run the agent against this project (Session.project_dir) '
            'instead of ember-code. The agent\'s CWD, codeindex, and '
            'workspace all point here. Used for cross-project evals.'
        ),
    )
    args = parser.parse_args()

    if args.no_codeindex:
        os.environ['EMBER_EVAL_NO_CODEINDEX'] = '1'

    ember_code_dir = Path(__file__).resolve().parent.parent
    # ``project_dir`` is what the agent's Session targets — its CWD,
    # codeindex, and workspace. ``yaml_root`` is where we read the
    # eval YAML + fixtures from. They're equal in the default case
    # (running ember-code's own evals against itself), and diverge
    # when ``--target-project-dir`` is set so we can eval the agent
    # against a different codebase (e.g. ember-server).
    yaml_root = ember_code_dir
    project_dir = args.target_project_dir.resolve() if args.target_project_dir else ember_code_dir
    # Make ember-code's evals/ importable so suites can reference
    # ``evals.<name>.setup`` regardless of which project_dir we're
    # targeting.
    if str(ember_code_dir) not in sys.path:
        sys.path.insert(0, str(ember_code_dir))

    from ember_code.core.config.settings import Settings
    from ember_code.core.evals.loader import load_eval_file
    from ember_code.core.evals.reporter import format_results
    from ember_code.core.evals.runner import SuiteResult
    from ember_code.core.session.core import Session

    yaml_path = yaml_root / 'evals' / f'{args.suite}.yaml'
    suite = load_eval_file(yaml_path)
    if suite is None:
        print(f'Failed to load suite from {yaml_path}', file=sys.stderr)
        return 1

    # CRITICAL: run the setup_module BEFORE constructing Session.
    # ``Session.__init__`` checks ``code_index.has_commit(HEAD)`` to
    # decide whether to expose the ``CodeIndex`` toolkit (and the
    # ``codeindex_query`` tool) to the agent. If we wait for
    # ``SuiteResult.run`` to invoke setup_module, the chroma dir
    # doesn't exist yet at Session creation, so the tool gate denies
    # exposure — and the agent never sees ``codeindex_query`` even
    # in WITH mode. Pre-running setup here flips the gate.
    if suite.setup_module:
        import importlib
        import inspect
        import tempfile

        prep_work_dir = Path(tempfile.mkdtemp(prefix='ember-eval-prep-'))
        try:
            module = importlib.import_module(suite.setup_module)
            setup_fn = getattr(module, 'setup', None)
            if setup_fn is not None:
                result = setup_fn(prep_work_dir, project_dir)
                if inspect.isawaitable(result):
                    await result
                print(
                    f'Pre-built codeindex via {suite.setup_module} (chroma populated for HEAD).',
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f'WARNING: pre-setup failed: {exc}', file=sys.stderr)
            # Continue anyway — the framework will try again; this
            # only matters for the tool-gate timing.

    settings = Settings()

    # Optional: enable Agno ``ReasoningTools`` (the ``think`` / ``analyze``
    # scratchpad) for this eval run. Toggled via ``EMBER_EVAL_REASONING=1``.
    # Used by v9 onwards to give the main agent a structured place to
    # commit to a reuse target before writing code.
    if os.environ.get('EMBER_EVAL_REASONING') == '1':
        settings.reasoning.enabled = True
        print('Reasoning tools enabled (think/analyze).', file=sys.stderr)

    # Wire the test API key into the model registry so the eval can run
    # without relying on a logged-in cloud-token. We override
    # ``models.default`` to a synthetic entry that points at the
    # EMBER_TEST_LLM_* env triple — same shape the Ember cloud uses
    # internally, just with explicit credentials.
    # Auto-source the project .env so callers don't have to remember
    # to ``set -a; source .env`` before each run. Lines are
    # ``KEY=VALUE`` (no quoting); skip blanks and comments.
    if not os.environ.get('EMBER_TEST_LLM_API_KEY'):
        env_path = ember_code_dir / '.env'
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                os.environ.setdefault(k, v)

    api_key = os.environ.get('EMBER_TEST_LLM_API_KEY')
    if api_key:
        base_url = os.environ.get('EMBER_TEST_LLM_BASE_URL', 'https://api.minimax.io/v1')
        model_id = os.environ.get('EMBER_TEST_LLM_MODEL', 'MiniMax-M2.7')
        settings.models.registry['eval-model'] = {
            'provider': 'openai_like',
            'model_id': model_id,
            'url': base_url,
            'api_key': api_key,
            'context_window': 200_000,
            'vision': False,
            # Hard cap per HTTP request. Without this, the openai SDK's
            # default (10 minutes) lets a single hung response wedge the
            # whole eval. Codewrite cases that loop through 20+ tool
            # turns can mask a single hang as "running" because each
            # turn is a fresh request.
            'timeout': 90,
        }
        settings.models.default = 'eval-model'
        # Force unbuffered stdout so per-case progress prints land in
        # the log immediately when launched as a background process.
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
        print(f'Using eval-model {model_id} @ {base_url} (timeout=90s)', file=sys.stderr)

    session = Session(settings, project_dir=project_dir)

    # The pool only knows the specialist agents loaded from .md files.
    # Eval suites that target ``agent: main`` need the main team
    # registered too. Inject it into the pool's cache so ``pool.get("main")``
    # returns the team Session built — same instance the TUI uses.
    if session.main_team is not None:
        session.pool._agents['main'] = session.main_team
        # Also stub a definition so ``pool.get`` doesn't trip the
        # "agent not found" branch when checking ``_definitions``.
        from ember_code.core.pool import AgentDefinition

        if 'main' not in session.pool._definitions:
            session.pool._definitions['main'] = (
                AgentDefinition(
                    name='main',
                    description='Main team (orchestrator).',
                ),
                999,
            )

    mode = 'WITHOUT codeindex' if args.no_codeindex else 'WITH codeindex'
    print(
        f'Running {len(suite.cases)} cases from {yaml_path.name} [{mode}] '
        f'against {project_dir}...',
        file=sys.stderr,
    )
    result = await SuiteResult.run(
        suite=suite,
        pool=session.pool,
        settings=settings,
        project_dir=project_dir,
    )

    print(format_results([result]))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        # Serialize per-case detail. Pydantic ``model_dump`` walks the
        # whole nested CaseResult / EvalCase tree so we get tool_trace,
        # response_text, judge reasons, timing — everything we need
        # for the comparison report.
        payload = {
            'mode': mode,
            'cases': [r.model_dump(mode='json') for r in result.case_results],
            'totals': {
                'passed': result.passed,
                'failed': result.failed,
                'total': result.total,
                'elapsed': result.elapsed,
            },
        }
        args.out.write_text(json.dumps(payload, indent=2, default=str))
        print(f'Detailed results written to {args.out}', file=sys.stderr)
    return 0 if result.failed == 0 else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
