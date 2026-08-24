from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AppSettings:
    league_config_path: Path
    database_url: str
    player_data_path: Path

    @classmethod
    def from_environment(cls) -> "AppSettings":
        return cls(
            league_config_path=Path(
                os.getenv("FANTASY_DRAFT_CONFIG", PROJECT_ROOT / "config" / "league.yaml")
            ),
            database_url=os.getenv(
                "FANTASY_DRAFT_DATABASE_URL",
                f"sqlite:///{PROJECT_ROOT / 'instance' / 'fantasy_draft.db'}",
            ),
            player_data_path=Path(
                os.getenv("FANTASY_DRAFT_PLAYERS", PROJECT_ROOT / "data" / "players.csv")
            ),
        )

