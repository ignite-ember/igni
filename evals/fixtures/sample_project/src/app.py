"""Top-level app entry point. Calls parse_config to load settings."""

import sys
import os
from typing import Optional

from src.parse import parse_config


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    config: Optional[dict] = parse_config(config_path) if os.path.exists(config_path) else None
    print(config)


if __name__ == "__main__":
    main()
