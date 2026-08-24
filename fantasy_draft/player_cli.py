from __future__ import annotations

import argparse
from pathlib import Path

from fantasy_draft.database import Base, create_database_engine, create_session_factory
from fantasy_draft.players import import_players
from fantasy_draft.settings import AppSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="Import or update canonical player data")
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args()
    settings = AppSettings.from_environment()
    engine = create_database_engine(settings.database_url)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    with session_factory.begin() as session:
        count = import_players(session, args.csv_path)
    print(f"Imported or updated {count} players.")
    engine.dispose()


if __name__ == "__main__":
    main()

