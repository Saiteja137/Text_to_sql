from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from .database import initialize_database


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env")
    parser = argparse.ArgumentParser(description="Create the sample college SQLite database.")
    parser.add_argument(
        "--database",
        default=os.getenv("DATABASE_PATH", str(project_root / "data" / "college.db")),
        help="Database file path (default: DATABASE_PATH or data/college.db)",
    )
    args = parser.parse_args()
    database_path = Path(args.database).expanduser()
    if not database_path.is_absolute():
        database_path = project_root / database_path
    database_path = database_path.resolve()
    initialize_database(database_path)
    print(f"College database is ready at: {database_path}")


if __name__ == "__main__":
    main()
