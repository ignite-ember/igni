"""igni — Terminal-based AI coding assistant built on Agno."""

# Must run before anything that pulls in ``transformers`` /
# ``sentence-transformers``. See the shim's docstring for why.
from ember_code import _torchvision_shim as _torchvision_shim  # noqa: F401

__version__ = "0.9.5"
