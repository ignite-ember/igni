"""The core's fixtures, and the cloud layer for the tests that want it."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from igni_cloud.layer import CloudLayer
from igni_cloud.logs import StdoutLog

from ember_code.core.layer import Layers
from tests.conftest import (  # noqa: F401 - fixtures, registered by being here
    _a_fresh_activity_registry,
    _desktop_storage,
    _isolate_user_settings,
    _no_extension_hooks_left_behind,
    database_url,
    postgres_url,
)


@pytest.fixture
def cloud_layer() -> Iterator[CloudLayer]:
    """The cloud layer installed, as ``IGNI_PROFILE=cloud`` would install it."""
    layer = CloudLayer()
    StdoutLog.reset()
    Layers.install(layer)
    yield layer
    Layers.reset()
    StdoutLog.reset()
