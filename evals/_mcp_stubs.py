"""Fake MCP-style tools for eval-only use.

Real MCP servers are out-of-process and per-project. To test agent
*routing* — does the model reach for ``mcp__linear__create_issue`` when
the user asks "open a Linear ticket"? — we don't need a real server, we
need tool *names* that look like MCP and a deterministic response.

The MCP naming convention is ``mcp__<server>__<tool>``. We expose four
common services (Linear, Notion, Slack, GitHub) with one canonical
write tool each. Each method returns a short canned success string so
the agent can reason about the result without real I/O.

Loaded only by the eval harness (see ``scripts/run_evals_smoke.py``).
Never wired into the production registry.
"""

from __future__ import annotations

from agno.tools import Toolkit


class MCPStubTools(Toolkit):
    """Eval-only stub of MCP-shaped tools.

    Every method follows the ``mcp__<server>__<action>`` naming so the
    LLM treats them the same as real MCP-surfaced tools. Bodies return
    a fixed string — these are routing tests, not integration tests.
    """

    def __init__(self, **kwargs):
        super().__init__(name="mcp_stubs", **kwargs)
        self.register(self.mcp__linear__create_issue)
        self.register(self.mcp__notion__create_page)
        self.register(self.mcp__slack__post_message)
        self.register(self.mcp__github__create_issue)

    def mcp__linear__create_issue(self, title: str, description: str = "") -> str:
        """Create a Linear issue. Returns the new issue's id and URL.

        Args:
            title: Issue title.
            description: Optional body text.
        """
        return f"Created Linear issue ENG-1234: '{title}' (https://linear.app/team/issue/ENG-1234)"

    def mcp__notion__create_page(self, title: str, content: str = "") -> str:
        """Create a Notion page in the workspace.

        Args:
            title: Page title.
            content: Markdown body.
        """
        return f"Created Notion page '{title}' (https://notion.so/page/abc123)"

    def mcp__slack__post_message(self, channel: str, text: str) -> str:
        """Post a message to a Slack channel.

        Args:
            channel: Channel name without the ``#`` prefix.
            text: Message body.
        """
        return f"Posted to #{channel}: '{text}' (ts=1730500000.000100)"

    def mcp__github__create_issue(self, repo: str, title: str, body: str = "") -> str:
        """Create a GitHub issue in the given repo.

        Args:
            repo: ``owner/name`` form.
            title: Issue title.
            body: Optional issue body.
        """
        return f"Created GitHub issue {repo}#42: '{title}' (https://github.com/{repo}/issues/42)"
