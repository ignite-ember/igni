"""Session package — interactive conversation loop with full subsystem integration."""

from ember_code.core.session.cloud_auth import SessionCloudAuth
from ember_code.core.session.cloud_catalog import CloudModelCatalog
from ember_code.core.session.codeindex_availability import (
    CodeIndexAvailabilityRefresher,
)
from ember_code.core.session.compaction import (
    CompactionCoordinator,
    ContextBreakdownReporter,
    FallbackSummariser,
)
from ember_code.core.session.core import Session
from ember_code.core.session.event_log import SessionEventLog
from ember_code.core.session.identity import SessionIdentity
from ember_code.core.session.interactive import run_session_interactive
from ember_code.core.session.knowledge_ops import SessionKnowledgeManager
from ember_code.core.session.learning_ops import SessionLearningManager
from ember_code.core.session.loop_ops import LoopController
from ember_code.core.session.mcp_ops import McpLifecycleCoordinator, McpLifecycleDeps
from ember_code.core.session.mcp_resolver import MCPToolResolver
from ember_code.core.session.memory_ops import SessionMemoryManager
from ember_code.core.session.message_handler import SessionMessageHandler
from ember_code.core.session.persistence import SessionPersistence
from ember_code.core.session.plan_ops import PlanCoordinator
from ember_code.core.session.plugin_reload import PluginReloadOrchestrator
from ember_code.core.session.reminders import PendingReminderQueue
from ember_code.core.session.run_debug import RunMessagesDebugDumper
from ember_code.core.session.runner import run_single_message
from ember_code.core.session.schemas import (
    CompactResult,
    ContextBreakdown,
    InteractiveBanner,
    LoopAdvance,
    LoopPhase,
    McpClientBundle,
    McpInitResult,
    McpServerStatus,
    MessageMedia,
    OutputStyleChangedBroadcast,
    PermissionModeChangedBroadcast,
    PlanDecidedBroadcast,
    PlanDecisionResult,
    PluginReloadCounts,
    PostCompactHookPayload,
    PreCompactHookPayload,
    SessionLifecyclePayload,
    StopFailureHookPayload,
    StopHookPayload,
    UserPromptSubmitHookPayload,
)
from ember_code.core.session.session_run import SessionRun
from ember_code.core.session.single_message_run import SingleMessageRun
from ember_code.core.session.startup import SessionStartupCoordinator
from ember_code.core.session.state_ops import (
    OutputStyleInstructionsPatcher,
    RuntimeModeCoordinator,
)
from ember_code.core.session.tool_hook_factory import ToolEventHookFactory

__all__ = [
    "CloudModelCatalog",
    "CodeIndexAvailabilityRefresher",
    "CompactResult",
    "CompactionCoordinator",
    "ContextBreakdown",
    "ContextBreakdownReporter",
    "FallbackSummariser",
    "InteractiveBanner",
    "LoopAdvance",
    "LoopController",
    "LoopPhase",
    "MCPToolResolver",
    "McpClientBundle",
    "McpInitResult",
    "McpLifecycleCoordinator",
    "McpLifecycleDeps",
    "McpServerStatus",
    "MessageMedia",
    "OutputStyleChangedBroadcast",
    "OutputStyleInstructionsPatcher",
    "PendingReminderQueue",
    "PermissionModeChangedBroadcast",
    "PlanCoordinator",
    "PlanDecidedBroadcast",
    "PlanDecisionResult",
    "PluginReloadCounts",
    "PluginReloadOrchestrator",
    "PostCompactHookPayload",
    "PreCompactHookPayload",
    "RunMessagesDebugDumper",
    "RuntimeModeCoordinator",
    "Session",
    "SessionCloudAuth",
    "SessionEventLog",
    "SessionIdentity",
    "SessionKnowledgeManager",
    "SessionLearningManager",
    "SessionLifecyclePayload",
    "SessionMemoryManager",
    "SessionMessageHandler",
    "SessionPersistence",
    "SessionRun",
    "SessionStartupCoordinator",
    "SingleMessageRun",
    "StopFailureHookPayload",
    "StopHookPayload",
    "ToolEventHookFactory",
    "UserPromptSubmitHookPayload",
    "run_session_interactive",
    "run_single_message",
]
