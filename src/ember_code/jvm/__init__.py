"""JVM/JDK bootstrap for Neo4j sidecar use.

Public surface: :func:`ensure_jdk`.
"""

from ember_code.backend.jdk_bootstrap import JdkBootstrap, JdkBootstrapError
from ember_code.jvm.ensure_jdk import ensure_jdk

__all__ = ["ensure_jdk", "JdkBootstrap", "JdkBootstrapError"]
