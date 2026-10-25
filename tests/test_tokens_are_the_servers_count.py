"""A conversation's size comes from the model server's own counts, not from a tokenizer.

agno counted the conversation with tiktoken before every model call
(its compression check) and for igni's context meter: ~80 MB resident
from the first message on, recounting what the server had counted.
Now the size is the last answer's measured input + output, plus a
length estimate for what came after it. These pin that the estimate
stays close to a real tokenizer, that compression triggers where it
did, and that nothing on igni's paths loads tiktoken again.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ember_code.core.session.compression import MeasuredCompression
from ember_code.core.session.token_estimate import TokenEstimate

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = json.loads((ROOT / "tests" / "fixtures" / "o200k_token_counts.json").read_text())[
    "samples"
]


def message(role: str, content: str, *, read: int = 0, wrote: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        role=role,
        content=content,
        compressed_content=None,
        tool_calls=None,
        metrics=SimpleNamespace(input_tokens=read, output_tokens=wrote),
    )


class TestTheEstimate:
    @pytest.mark.parametrize("sample", SAMPLES, ids=[s["kind"] for s in SAMPLES])
    def test_uncounted_text_lands_within_fifteen_percent_of_a_real_tokenizer(
        self, sample: dict
    ) -> None:
        estimate = TokenEstimate.chars([message("tool", sample["text"])])
        assert estimate == pytest.approx(sample["o200k_tokens"], rel=0.15)

    def test_the_server_s_count_covers_everything_up_to_the_last_answer(self) -> None:
        conversation = [
            message("system", "s" * 9000),
            message("user", "u" * 4000),
            message("assistant", "a" * 800, read=3400, wrote=210),
        ]
        assert TokenEstimate.of(conversation) == 3610

    def test_only_what_came_after_the_last_answer_is_estimated(self) -> None:
        conversation = [
            message("system", "s" * 9000),
            message("assistant", "first", read=1000, wrote=50),
            message("tool", "t" * 4000),
            message("assistant", "second", read=2300, wrote=120),
            message("tool", "r" * 800),
        ]
        assert TokenEstimate.of(conversation) == 2300 + 120 + 200

    def test_a_conversation_nobody_has_counted_is_estimated_whole_with_its_tools(self) -> None:
        tools = [{"name": "read_file", "description": "d" * 396}]
        conversation = [message("system", "s" * 400), message("user", "u" * 400)]
        tool_chars = len(json.dumps(tools[0]))
        assert TokenEstimate.of(conversation, tools) == 200 + round(tool_chars / 4)


class TestCompressionTriggersWhereItDid:
    """agno compared a tokenizer's count with the limit; now the server's count is compared."""

    LIMIT = 8000

    def test_at_the_limit_it_compresses_and_just_under_it_does_not(self) -> None:
        compression = MeasuredCompression(compress_token_limit=self.LIMIT)
        model = object()
        over = [message("assistant", "a", read=7700, wrote=200), message("tool", "x" * 400)]
        under = [message("assistant", "a", read=7600, wrote=200), message("tool", "x" * 400)]
        assert compression.should_compress(over, model=model)
        assert not compression.should_compress(under, model=model)

    def test_tool_results_appended_one_by_one_trigger_on_the_same_one_as_a_tokenizer(
        self,
    ) -> None:
        """The baseline adds each result's real o200k count; the estimate adds its length / 4."""
        base_tokens = 5000
        conversation = [message("assistant", "a", read=base_tokens - 100, wrote=100)]
        real = base_tokens
        compression = MeasuredCompression(compress_token_limit=self.LIMIT)
        real_trigger = estimated_trigger = None
        for i, sample in enumerate(SAMPLES * 2):
            conversation.append(message("tool", sample["text"]))
            real += sample["o200k_tokens"]
            if real_trigger is None and real >= self.LIMIT:
                real_trigger = i
            if estimated_trigger is None and compression.should_compress(
                conversation, model=object()
            ):
                estimated_trigger = i
        assert real_trigger is not None and estimated_trigger is not None
        assert abs(real_trigger - estimated_trigger) <= 1

    async def test_the_async_check_is_the_same_decision(self) -> None:
        compression = MeasuredCompression(compress_token_limit=self.LIMIT)
        over = [message("assistant", "a", read=8100, wrote=0)]
        assert await compression.ashould_compress(over, model=object())

    def test_the_tool_count_threshold_still_applies(self) -> None:
        compression = MeasuredCompression(compress_tool_results_limit=2)
        two = [message("tool", "x"), message("tool", "y")]
        assert compression.should_compress(two)
        assert not compression.should_compress(two[:1])


class TestNothingLoadsTiktoken:
    def test_no_igni_module_imports_it_or_asks_a_model_to_count(self) -> None:
        offenders: list[str] = []
        for path in (ROOT / "src" / "ember_code").rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import | ast.ImportFrom):
                    names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                    if any(n.split(".")[0] == "tiktoken" for n in names):
                        offenders.append(f"{path}: imports tiktoken")
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in ("count_tokens", "acount_tokens")
                    and isinstance(node.ctx, ast.Load)
                ):
                    offenders.append(f"{path}:{node.lineno}: asks a model to count")
        assert offenders == []

    def test_the_session_builds_its_agent_with_the_measured_compression(self) -> None:
        source = (ROOT / "src" / "ember_code" / "core" / "session" / "core.py").read_text()
        assert "compression_cls=MeasuredCompression" in source
        assert "CompressionManager" not in source

    def test_a_compression_check_on_a_real_model_leaves_tiktoken_unloaded(self) -> None:
        script = (
            "import sys\n"
            "from types import SimpleNamespace as N\n"
            "from agno.models.openai.like import OpenAILike\n"
            "from ember_code.core.session.compression import MeasuredCompression\n"
            "m = OpenAILike(id='gpt-4o', api_key='x')\n"
            "msgs = [N(role='user', content='hi '*5000, compressed_content=None,"
            " tool_calls=None, metrics=N(input_tokens=0, output_tokens=0))]\n"
            "assert MeasuredCompression(compress_token_limit=10).should_compress(msgs, model=m)\n"
            "assert 'tiktoken' not in sys.modules, 'tiktoken loaded'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stderr
