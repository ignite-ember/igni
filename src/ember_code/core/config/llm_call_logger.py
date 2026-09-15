"""Dedicated LLM-call logger.

Extracted from the module-import-time filesystem side effect that
used to live at the top of ``models.py``. Now a real class that
``ModelRegistry`` constructs and passes into every ``LoggingModel``
via ``OpenAILikeBuilder`` — no more logger side effects on import,
no more global mutable state pretending to be constants.

Preserves the important behavior the original comment called out:
* propagates to root so ``--debug`` runs land LLM entries in
  ``~/.igni/debug.log`` alongside diagnostics (cross-referencing
  the FE timeline with BE lifecycle traces relies on this).
* attaches the same handler to the ``httpx`` and ``httpcore``
  loggers so hung-connection lifecycle events land in the same file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ember_code.core.config.caller_inspector import CallerContextInspector
from ember_code.core.paths import DEFAULT_DATA_DIR


class LlmCallLogger:
    """Owns the ``~/.igni/llm_calls.log`` handler and the httpx /
    httpcore log-propagation setup.

    Instantiated inside :class:`ModelRegistry.__init__`; injected
    into every ``LoggingModel`` via ``OpenAILikeBuilder``. Lazy
    handler install — the log file/directory is created on FIRST
    ``log_call`` (or explicit ``ensure_configured``) call, not on
    import.
    """

    def __init__(
        self,
        log_dir: str | Path | None = None,
        caller_inspector: CallerContextInspector | None = None,
    ) -> None:
        self._log_dir = (
            Path(os.path.expanduser(str(log_dir)))
            if log_dir
            else Path(os.path.expanduser(DEFAULT_DATA_DIR))
        )
        self._logger = logging.getLogger("ember_code.llm_calls")
        self._caller_inspector = caller_inspector or CallerContextInspector()
        self._configured = False

    def ensure_configured(self) -> None:
        """Idempotent handler install.

        Safe to call from every ``log_call`` — the ``_configured``
        flag short-circuits after the first successful install.
        Re-checking ``self._logger.handlers`` also covers the case
        where a fresh registry inherits a logger already set up by
        an earlier instance in the same process.
        """
        if self._configured or self._logger.handlers:
            self._configured = True
            return
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_dir / "llm_calls.log"
        handler = logging.FileHandler(log_path)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
        self._logger.addHandler(handler)
        self._logger.setLevel(logging.INFO)
        # File only, for the same reason as the transport loggers below — and
        # more urgently, because this logger writes full request *and* response
        # bodies (which is why llm_calls.log reaches hundreds of MB). Propagating
        # put all of that on whatever handler root happened to have; against a
        # pipe nobody drains, the emit blocks on the event loop and the agent
        # wedges after the model has already answered.
        #
        # ``--debug`` used to get LLM entries via this propagation, so it now
        # attaches its handler to this logger directly (``DebugLogging.enable``)
        # rather than relying on the root path.
        self._logger.propagate = False

        # Attach the same handler to httpx/httpcore to capture
        # connection lifecycle. Hung LLM calls are almost always
        # network stalls; without these we can't distinguish "model
        # is thinking" from "TCP wedged".
        self._attach_transport_loggers(handler)
        self._configured = True

    def _attach_transport_loggers(self, handler: logging.Handler) -> None:
        for name in ("httpx", "httpcore"):
            transport_logger = logging.getLogger(name)
            transport_logger.addHandler(handler)
            transport_logger.setLevel(logging.DEBUG)
            # File only. ``callHandlers`` walks to root and checks each
            # *handler's* level, never the ancestor loggers' — so root's
            # WARNING does not filter these, and alembic's ``fileConfig``
            # leaves a NOTSET stderr handler there (see
            # ``Migrator.upgrade_to_head``). Left propagating, one streaming
            # call put thousands of DEBUG records on stderr; against a pipe
            # nobody drains, the emit blocks inside httpcore on the event loop
            # and the whole agent wedges silently at ~64 KB.
            transport_logger.propagate = False

    def log_call(
        self,
        method: str,
        *,
        model_id: str,
        n_messages: int,
        stream: bool,
        url: str,
        caller_depth: int = 3,
    ) -> None:
        """Emit one ``LLM call: ...`` log line.

        ``caller_depth`` controls how far up the stack the
        ember_code frame walk starts. Default of 3 skips this
        method + the caller in ``LoggingModel`` + the SDK internal
        that triggered the call — matches the layout in the old
        inline implementation.
        """
        self.ensure_configured()
        caller = self._caller_inspector.format_caller_chain(depth=caller_depth)
        self._logger.info(
            "LLM call: %s | model=%s | messages=%d | stream=%s | url=%s | caller=%s",
            method,
            model_id,
            n_messages,
            stream,
            url,
            caller,
        )
