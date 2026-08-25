from __future__ import annotations

import json
from datetime import date

from sqlalchemy import Engine, inspect, text

from fantasy_draft.database import Base


SCHEMA_VERSION = 2


def _columns(engine: Engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def _add_column(engine: Engine, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(engine, table):
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {definition}"))


def run_migrations(engine: Engine) -> None:
    """Apply small, forward-only SQLite-compatible schema upgrades in place."""
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        ))

    _add_column(engine, "drafts", "season INTEGER")
    _add_column(engine, "drafts", "draft_kind VARCHAR(20) NOT NULL DEFAULT 'practice'")
    _add_column(engine, "drafts", "data_snapshot JSON NOT NULL DEFAULT '{}'")
    _add_column(engine, "drafts", "completed_at DATETIME")
    _add_column(engine, "drafts", "archived_at DATETIME")
    _add_column(engine, "players", "status VARCHAR(30)")
    _add_column(engine, "players", "birth_date DATE")
    _add_column(engine, "players", "draft_year INTEGER")
    _add_column(engine, "draft_picks", "market_adp FLOAT")
    _add_column(engine, "draft_picks", "market_rank FLOAT")

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE drafts SET season = :season WHERE season IS NULL"),
            {"season": date.today().year},
        )
        latest = connection.execute(
            text("SELECT id FROM drafts WHERE status IN ('setup','active','completed') ORDER BY id DESC LIMIT 1")
        ).scalar()
        current = connection.execute(text("SELECT current_draft_id FROM application_state WHERE id=1")).scalar()
        if current is None and latest is not None:
            connection.execute(
                text("INSERT OR REPLACE INTO application_state (id,current_draft_id) VALUES (1,:draft_id)"),
                {"draft_id": latest},
            )

        rows = connection.execute(text("SELECT id, external_ids FROM players WHERE external_ids IS NOT NULL"))
        for player_id, raw in rows:
            ids = json.loads(raw) if isinstance(raw, str) else (raw or {})
            for provider, external_id in ids.items():
                if external_id:
                    connection.execute(
                        text(
                            "INSERT OR IGNORE INTO player_external_ids "
                            "(player_id,provider,external_id,source,verified,created_at) "
                            "VALUES (:player,:provider,:external,'legacy-json',1,CURRENT_TIMESTAMP)"
                        ),
                        {"player": player_id, "provider": provider.lower(), "external": str(external_id)},
                    )
        connection.execute(
            text("INSERT OR IGNORE INTO schema_migrations (version) VALUES (:version)"),
            {"version": SCHEMA_VERSION},
        )
