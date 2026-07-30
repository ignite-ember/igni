"""Configuration management for igni."""

from ember_code.core.config.cloud_model_migrator import CloudModelMigrator
from ember_code.core.config.managed_policy import ManagedPolicySource
from ember_code.core.config.settings import Settings, load_settings
from ember_code.core.config.settings_loader import SettingsLoader
from ember_code.core.config.user_config_store import UserConfigStore

__all__ = [
    "CloudModelMigrator",
    "ManagedPolicySource",
    "ModelRegistry",
    "Settings",
    "SettingsLoader",
    "UserConfigStore",
    "load_settings",
]


def __getattr__(name: str):
    if name == "ModelRegistry":
        from ember_code.core.config.models import ModelRegistry

        return ModelRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
