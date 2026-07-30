"""Message-type registry — owns the ``type`` string → ``Message`` subclass map.

Previously this responsibility was smeared across three module-level
symbols in ``transport/unix_socket.py`` (a mutable dict, a
``_build_registry`` initializer that inline-imported
``protocol.messages``, and a ``deserialize_message`` free function that
called into that dict). All three collapse into :class:`MessageRegistry`,
whose ``__init__`` eagerly scans ``protocol.messages`` for
:class:`Message` subclasses with a defaulted ``type`` field and stores
the mapping on an instance attribute.

The registry is used by every stream-framed transport
(:class:`~ember_code.transport.ndjson_stream.NDJsonStreamTransport` and
:class:`~ember_code.transport.websocket.WebSocketServerTransport`). Each
transport is constructor-injectable so tests can pass a fresh
:class:`MessageRegistry` for isolation, and callers that don't care can
use :meth:`MessageRegistry.default` for the process-shared singleton.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from ember_code.protocol import messages as msg_module
from ember_code.protocol.messages import Message

logger = logging.getLogger(__name__)


class MessageRegistry:
    """Lookup table from wire ``type`` string to :class:`Message` subclass.

    Reflection-populated at construction time from the classes exported
    by :mod:`ember_code.protocol.messages`: any subclass of
    :class:`Message` with a defaulted ``type`` field is enrolled under
    that default. Subclasses defined outside that module can be
    registered explicitly via :meth:`register`.

    Instances are cheap; a process-shared default is available via
    :meth:`default`. Per-test isolation is achieved by constructing a
    fresh ``MessageRegistry()`` and threading it into the transport.
    """

    _default: MessageRegistry | None = None

    def __init__(self) -> None:
        self._by_type: dict[str, type[Message]] = {}
        for name in dir(msg_module):
            cls = getattr(msg_module, name)
            if (
                isinstance(cls, type)
                and issubclass(cls, Message)
                and cls is not Message
                and hasattr(cls, "model_fields")
            ):
                type_field = cls.model_fields.get("type")
                # Skip abstract intermediaries that inherit ``type: str``
                # from :class:`Message` without defaulting it (e.g.
                # :class:`RunScopedMessage`). Pydantic represents an
                # un-defaulted field with ``PydanticUndefined`` — a
                # sentinel that is truthy but not a wire string, so
                # ``isinstance`` on ``str`` filters it cleanly.
                if type_field and isinstance(type_field.default, str):
                    self._by_type[type_field.default] = cls

    @classmethod
    def default(cls) -> MessageRegistry:
        """Return the process-shared registry, constructing it on first use.

        Callers that need isolation (typically tests) should construct a
        fresh ``MessageRegistry()`` instead.
        """
        if cls._default is None:
            cls._default = cls()
        return cls._default

    def register(self, message_cls: type[Message]) -> None:
        """Enrol a :class:`Message` subclass defined outside ``protocol.messages``.

        Reads the class's defaulted ``type`` field the same way the
        constructor does. Raises ``ValueError`` if the class has no
        default ``type`` — a registry entry needs a wire string to key on.
        """
        type_field = message_cls.model_fields.get("type")
        if not (type_field and type_field.default):
            raise ValueError(
                f"{message_cls.__name__} has no default 'type' field; "
                "cannot register without a wire discriminator"
            )
        self._by_type[type_field.default] = message_cls

    def deserialize(self, line: str) -> Message | None:
        """Deserialize a JSON line into a :class:`Message`.

        Returns ``None`` (with a warning log) on any of:

        * malformed JSON
        * a ``type`` string not present in the registry
        * a payload that fails Pydantic validation against the resolved
          subclass

        The transports treat ``None`` as "skip this frame" so a bad
        message never wedges the read loop.
        """
        try:
            data = json.loads(line)
            msg_type = data.get("type", "")
            cls = self._by_type.get(msg_type)
            if cls is None:
                logger.warning("Unknown message type: %s", msg_type)
                return None
            return cls.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("Failed to deserialize message: %s", exc)
            return None
