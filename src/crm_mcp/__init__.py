"""CRM MCP server package."""

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Read KEY=VALUE lines from .env (project root, then cwd). Real env vars win."""
    for path in (Path(__file__).resolve().parents[2] / ".env", Path.cwd() / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# Runs before any submodule reads its settings.
_load_dotenv()
