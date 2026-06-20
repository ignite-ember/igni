"""Default configuration values."""

DEFAULT_CONFIG = {
    "api_url": "https://api.ignite-ember.sh",
    "update_check_ttl": 0,
    # Empty by design. Hosted models come from cloud discovery
    # (``GET /v1/chat/models`` — see ``cloud_models.py``), and the
    # active model defaults to the first entry in the merged registry.
    # Users add their own overrides in ``~/.ember/config.yaml``; we
    # never ship hardcoded cloud entries so a model bump on the
    # server doesn't require a client release or a migration.
    "models": {
        "registry": {},
    },
    "permissions": {
        "file_read": "allow",
        "file_write": "ask",
        "shell_execute": "ask",
        "shell_restricted": "allow",
        "web_search": "allow",
        "web_fetch": "allow",
        "git_push": "ask",
        "git_destructive": "ask",
    },
    "safety": {
        "protected_paths": [
            ".env",
            ".env.*",
            "*.pem",
            "*.key",
            "credentials.*",
            "secrets.*",
        ],
        "blocked_commands": [
            "rm -rf /",
            ":(){ :|:& };:",
        ],
        "max_file_size_kb": 500,
        "require_confirmation": [
            "git push",
            "git push --force",
            "npm publish",
            "pip install",
            "docker run",
            "terraform apply",
            "kubectl apply",
            "kubectl delete",
        ],
    },
    "storage": {
        "data_dir": "~/.ember",
        "audit_log": "~/.ember/audit.log",
        "max_history_runs": 10000,
    },
    "rules": {
        "cross_tool_support": True,
    },
    "hooks": {
        "cross_tool_support": True,
    },
    "context": {
        "project_file": "ember.md",
        "ignore_patterns": [
            "node_modules/",
            ".git/",
            "__pycache__/",
            "*.pyc",
            ".venv/",
            "dist/",
            "build/",
        ],
    },
    "orchestration": {
        "max_nesting_depth": 5,
        "max_total_agents": 20,
        "sub_team_timeout": 1800,
        "max_task_iterations": 10,
        "generate_ephemeral": True,
        "max_ephemeral_per_session": 5,
        "auto_cleanup": True,
    },
    "learning": {
        "enabled": True,
        # Auto-extraction blobs left off by default — they each run a
        # separate post-stream LLM call that added 5–10s to the tail
        # between ``streaming_done`` and ``run_completed``. The
        # agentic ``user_memory`` tool path covers the same intent
        # on demand (agent decides what's worth saving).
        "user_profile": False,
        "user_memory": True,
        "session_context": False,
        "entity_memory": False,
        "learned_knowledge": False,
    },
    "reasoning": {
        "enabled": False,
        "add_instructions": True,
        "add_few_shot": False,
    },
    "guardrails": {
        "pii_detection": False,
        "prompt_injection": False,
        "moderation": False,
    },
    "knowledge": {
        "enabled": True,
        "collection_name": "ember_knowledge",
        "max_results": 10,
    },
    "agents": {
        "cross_tool_support": True,
    },
    "skills": {
        "cross_tool_support": True,
        "auto_trigger": True,
    },
    "scheduler": {
        "poll_interval": 30,
        "task_timeout": 300,
        "max_concurrent": 1,
    },
    "auth": {
        "credentials_file": "~/.ember/credentials.json",
    },
    "display": {
        "markdown": True,
        "show_tool_calls": True,
        "show_routing": False,
        "show_reasoning": False,
        "color_theme": "auto",
    },
}
