"""When to compress tool results, decided from the server's own token counts.

agno's :class:`~agno.compression.manager.CompressionManager` asks the
model to count the conversation before every model call
(``model.count_tokens``), which loads tiktoken's encoding — about
80 MB resident for the rest of the process, to recount text the server
already counted. This subclass makes the same threshold decision from
:class:`~ember_code.core.session.token_estimate.TokenEstimate` instead;
compressing itself is agno's, unchanged.
"""

from __future__ import annotations

from typing import Any

from agno.compression.manager import CompressionManager

from ember_code.core.session.token_estimate import TokenEstimate


class MeasuredCompression(CompressionManager):
    """agno's compression, with the token threshold read off the server's counts."""

    def should_compress(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        model: Any = None,
        response_format: Any = None,
    ) -> bool:
        if not self.compress_tool_results:
            return False
        if (
            self.compress_token_limit is not None
            and model is not None
            and TokenEstimate.of(messages, tools) >= self.compress_token_limit
        ):
            return True
        return self._over_tool_count(messages)

    async def ashould_compress(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        model: Any = None,
        response_format: Any = None,
    ) -> bool:
        return self.should_compress(messages, tools, model, response_format)

    def _over_tool_count(self, messages: list[Any]) -> bool:
        if self.compress_tool_results_limit is None:
            return False
        uncompressed = [
            m for m in messages if self._is_tool_result_message(m) and m.compressed_content is None
        ]
        return len(uncompressed) >= self.compress_tool_results_limit


__all__ = ["MeasuredCompression"]
