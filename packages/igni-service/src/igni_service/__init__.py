"""igni-service: what a service needs to be part of an igni deployment.

    igni = IgniService.from_settings(IgniSettings())
    app = FastAPI(lifespan=igni.lifespan)
    app.include_router(igni.router)

See the package README for the whole surface.
"""

from igni_service.directory import Directory, Member, Organisation
from igni_service.errors import (
    BundleError,
    ConfigurationError,
    IgniError,
    IgniUnavailable,
    KeyStoreError,
    NotificationRefused,
)
from igni_service.host import Host
from igni_service.keystore import KeyCipher, KeyStore, MemoryKeyStore, StoredKey
from igni_service.notifications import Delivered, Notification, Target
from igni_service.service import IgniService
from igni_service.settings import IgniSettings
from igni_service.sql_keystore import SqlKeyStore
from igni_service.subjects import ExportSection, Subject
from igni_service.tables import IgniTables
from igni_service.tokens import IgniCall, Person, TokenRefused
from igni_service.web import WebSignIn
from igni_service.web_sessions import (
    MemoryWebSessionStore,
    SqlWebSessionStore,
    WebSession,
    WebSessionStore,
)

__all__ = [
    "BundleError",
    "ConfigurationError",
    "Delivered",
    "Directory",
    "ExportSection",
    "Host",
    "IgniCall",
    "IgniError",
    "IgniService",
    "IgniSettings",
    "IgniTables",
    "IgniUnavailable",
    "KeyCipher",
    "KeyStore",
    "KeyStoreError",
    "Member",
    "MemoryKeyStore",
    "MemoryWebSessionStore",
    "Notification",
    "NotificationRefused",
    "Organisation",
    "Person",
    "SqlKeyStore",
    "SqlWebSessionStore",
    "StoredKey",
    "Subject",
    "Target",
    "TokenRefused",
    "WebSession",
    "WebSessionStore",
    "WebSignIn",
]
