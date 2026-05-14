from __future__ import annotations

import os
from pathlib import Path

DEFAULT_WINDOWS_ROOT = r"C:\Users\LiamDoherty\B-First Fire Safety Dropbox\8. Horse Project\website_2_0"


def project_root() -> Path:
    """Return the project root.

    On Liam's machine this should be the folder containing this project. You can
    also set RACING_PROJECT_ROOT if you ever move it.
    """
    env = os.getenv("RACING_PROJECT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


ROOT = project_root()
DATA_INBOX = ROOT / "data_inbox"

# Folder-based workflow.
HISTORICAL_RESULTS_INBOX = DATA_INBOX / "historical_results"
DAILY_RESULTS_INBOX = DATA_INBOX / "daily_results"
DAILY_RACECARDS_INBOX = DATA_INBOX / "daily_racecards"

# Legacy folder names from earlier versions. These are still scanned so old habits/files keep working.
LEGACY_RESULTS_INBOX = DATA_INBOX / "results"
LEGACY_RACECARDS_INBOX = DATA_INBOX / "racecards"

# Main folders used by the app.
RESULTS_INBOX = DAILY_RESULTS_INBOX
RACECARDS_INBOX = DAILY_RACECARDS_INBOX

DATABASE_DIR = ROOT / "database"
DB_PATH = DATABASE_DIR / "racing.db"
OUTPUTS_DIR = ROOT / "outputs"
LOGS_DIR = ROOT / "logs"
PROCESSED_DIR = DATA_INBOX / "processed"

ADMIN_PASSWORD = os.getenv("RACING_ADMIN_PASSWORD", "admin123")

for folder in [
    HISTORICAL_RESULTS_INBOX,
    DAILY_RESULTS_INBOX,
    DAILY_RACECARDS_INBOX,
    LEGACY_RESULTS_INBOX,
    LEGACY_RACECARDS_INBOX,
    RESULTS_INBOX,
    RACECARDS_INBOX,
    DATABASE_DIR,
    OUTPUTS_DIR,
    LOGS_DIR,
    PROCESSED_DIR,
]:
    folder.mkdir(parents=True, exist_ok=True)
