"""Load environment configuration before any application module reads it."""

import os
from pathlib import Path

from dotenv import load_dotenv

# An explicit path also allows isolated tests and alternate deployments. Exported
# environment variables always take precedence over values in this file.
ENV_FILE = Path(
    os.environ.get(
        "EXPENSO_ENV_FILE", str(Path(__file__).resolve().parent.parent / ".env")
    )
)
load_dotenv(ENV_FILE, override=False)
