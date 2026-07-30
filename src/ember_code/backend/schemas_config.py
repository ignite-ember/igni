"""Typed view model for the ``/config`` slash command's chat output.

Extracted from :mod:`ember_code.backend.command_handler` — the
old ``_cmd_config`` inline body built a 20-line markdown template
inside the handler, reaching into ``session.settings`` and calling
the free ``load_credentials`` directly. :class:`ConfigView` now
owns every rendered field so the coordinator becomes
``ConfigView.from_session(session).to_command_result()`` — mirrors
the :mod:`schemas_codeindex` pattern.

The credentials read routes through a
:class:`~ember_code.core.auth.credentials.CredentialsStore` (default
constructed inside :meth:`from_session`); tests can inject a fake
store via the ``store`` kwarg instead of monkey-patching a
module-level free function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from ember_code.backend.command_result import CommandResult
from ember_code.core.auth.credentials import CredentialsStore

if TYPE_CHECKING:
    from ember_code.core.session import Session


class ConfigView(BaseModel):
    """Every field rendered by ``/config``, materialized into a
    Pydantic model. :meth:`from_session` is the constructor;
    :meth:`to_command_result` renders the 21-line markdown block.
    """

    model: str
    auth_status: str
    file_write: str
    shell_execute: str
    storage_backend: str
    learning_enabled: bool
    reasoning_enabled: bool
    guardrail_pii: bool
    guardrail_injection: bool
    guardrail_moderation: bool
    knowledge_enabled: bool
    max_agents: int
    max_depth: int
    session_id: str

    @classmethod
    def from_session(
        cls,
        session: Session,
        store: CredentialsStore | None = None,
    ) -> ConfigView:
        """Snapshot the rendered fields off the live :class:`Session`.

        The credentials read uses the injected ``store`` (or a
        default :class:`CredentialsStore`) so tests can point at
        a tmp path via constructor injection instead of patching
        the sibling ``command_handler`` module.
        """
        s = session.settings
        creds_store = store or CredentialsStore()
        result = creds_store.load()
        creds = result.creds if result.ok else None
        if creds is not None and not creds.is_expired():
            auth_status = creds.email or "logged in"
        else:
            auth_status = "not logged in"
        return cls(
            model=s.models.default,
            auth_status=auth_status,
            file_write=s.permissions.file_write,
            shell_execute=s.permissions.shell_execute,
            storage_backend=s.storage.backend,
            learning_enabled=s.learning.enabled,
            reasoning_enabled=s.reasoning.enabled,
            guardrail_pii=s.guardrails.pii_detection,
            guardrail_injection=s.guardrails.prompt_injection,
            guardrail_moderation=s.guardrails.moderation,
            knowledge_enabled=s.knowledge.enabled,
            max_agents=s.orchestration.max_total_agents,
            max_depth=s.orchestration.max_nesting_depth,
            session_id=session.session_id,
        )

    def to_command_result(self) -> CommandResult:
        guardrails_bits = ""
        if self.guardrail_pii:
            guardrails_bits += "PII "
        if self.guardrail_injection:
            guardrails_bits += "injection "
        if self.guardrail_moderation:
            guardrails_bits += "moderation "
        if not (self.guardrail_pii or self.guardrail_injection or self.guardrail_moderation):
            guardrails_bits += "(none)"
        return CommandResult.markdown(
            "## Configuration\n"
            f"- **Model:** {self.model}\n"
            f"- **Auth:** {self.auth_status}\n"
            f"- **Permissions:** file_write={self.file_write}, "
            f"shell={self.shell_execute}\n"
            f"- **Storage:** {self.storage_backend}\n"
            f"- **Learning:** {'enabled' if self.learning_enabled else 'disabled'}\n"
            f"- **Reasoning tools:** {'enabled' if self.reasoning_enabled else 'disabled'}\n"
            f"- **Guardrails:** {guardrails_bits}\n"
            f"- **Knowledge:** {'enabled' if self.knowledge_enabled else 'disabled'}\n"
            f"- **Compression:** enabled\n"
            f"- **Session summaries:** enabled\n"
            f"- **Max agents:** {self.max_agents}\n"
            f"- **Max depth:** {self.max_depth}\n"
            f"- **Session:** {self.session_id}\n"
        )


__all__ = ["ConfigView"]
