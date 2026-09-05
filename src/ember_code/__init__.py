"""igni — Terminal-based AI coding assistant built on Agno."""

import os

# Must run before anything that pulls in ``transformers`` /
# ``sentence-transformers``. See the shim's docstring for why.
from ember_code import _torchvision_shim as _torchvision_shim  # noqa: F401

# ── No run leaves the machine unless somebody asks for it ──────────
#
# Agno posts a telemetry event to ``https://os-api.agno.com`` on every
# agent run, and its default is on. Found by reading a ``--debug`` log
# during a smoke test: ``POST https://os-api.agno.com/telemetry/runs
# "HTTP/2 201 Created"``, once per run, on an ordinary session.
#
# What it sends is metadata, not content: agent/session/run ids, the
# provider, model name and model id, the database class name, and a set
# of capability booleans. **No prompt, no code, no paths.** That
# distinction matters and is why this is not a breach of "your code
# never leaves your network" — but it is still an unannounced outbound
# call from a product sold as self-hosted, it names the customer's
# model and storage choices, and it is a third party that appears in
# none of the compliance documents that enumerate where data goes.
#
# Set here rather than at each ``Agent(...)`` / ``Team(...)`` because
# there are a dozen construction sites across agents, pools, ephemeral
# builders and the orchestrator, and one missed site is a silent leak.
# ``agno.agent._init.set_telemetry`` re-reads this variable on every
# run and overrides whatever the constructor said, so one assignment
# covers every path including ones added later.
#
# An explicit value is honoured. Somebody who *wants* to send telemetry
# can, which is the same principle the deployment docs take elsewhere:
# nothing leaves by default, and every means to send it is available.
os.environ.setdefault("AGNO_TELEMETRY", "false")

__version__ = "1.0.3"
