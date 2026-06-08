"""Disarm a transitive-dep crash that breaks every ``transformers`` import.

``torchvision`` registers fake implementations for ops like
``torchvision::nms`` at module load time:

    @torch.library.register_fake("torchvision::nms")

On torch / torchvision wheel pairs whose native bindings don't match
(seen on macOS arm64 + Python 3.12 with torch 2.11.0 + torchvision
0.26.0 / 0.27.0), that registration raises
``RuntimeError: operator torchvision::nms does not exist``.
``transformers`` then catches the failure inside its lazy
``__getattr__`` and re-raises it as a misleading
``ModuleNotFoundError: Could not import module 'PreTrainedModel'``,
which kills sentence-transformers, chroma embeddings, and the
CodeIndex ``apply_delta`` path along with it.

We don't use torchvision — ember-code embeds text only. The fix is to
make sure ``transformers`` never reaches torchvision's auto-discovery
in the first place. Eagerly probe the import; if it explodes, mark
the module unimportable in ``sys.modules`` so future ``import
torchvision`` raises a clean ``ImportError``. ``transformers``'s
``is_torchvision_available()`` treats that as "not installed" and
takes the text-only path.

This module is import-side-effect-only — import it once, at the top
of ``ember_code/__init__.py``, before anything that pulls in
``transformers`` / ``sentence-transformers``.
"""

import importlib.util
import logging
import sys

logger = logging.getLogger(__name__)


def _disarm_torchvision_if_broken() -> None:
    if "torchvision" in sys.modules:
        # Already imported — either successfully (nothing to do) or by
        # something that handled the failure itself.
        return
    try:
        spec = importlib.util.find_spec("torchvision")
    except Exception:
        # ``find_spec`` itself can raise on partial installs. Treat as
        # missing — transformers will skip torchvision regardless.
        return
    if spec is None:
        return
    try:
        import torchvision  # noqa: F401 — eager probe; failure is the signal
    except Exception as exc:
        logger.info(
            "torchvision import failed (%s); marking it unimportable so "
            "transformers takes the text-only path",
            exc,
        )
        # ``None`` in sys.modules makes future ``import torchvision``
        # raise ``ImportError: import of torchvision halted; None in
        # sys.modules`` — which ``importlib.util.find_spec`` and
        # transformers' availability check both translate to "not
        # available".
        sys.modules["torchvision"] = None  # type: ignore[assignment]


_disarm_torchvision_if_broken()
