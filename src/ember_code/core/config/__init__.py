"""Configuration management for igni."""

from ember_code.core.config.settings import Settings, load_settings

__all__ = ["Settings", "load_settings", "ModelRegistry"]


def __getattr__(name: str):
    if name == "ModelRegistry":
        from ember_code.core.config.models import ModelRegistry

        return ModelRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
