"""Create database tables (idempotent). The app also does this on startup.

    python scripts/init_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.database.session import Database  # noqa: E402


def main() -> None:
    settings = get_settings()
    db = Database(settings.database_url)
    db.create_all()
    db.dispose()
    # Print the scheme only: the URL may contain credentials (PostgreSQL).
    print(f"Database initialised ({settings.database_url.split(':', 1)[0]})")


if __name__ == "__main__":
    main()
