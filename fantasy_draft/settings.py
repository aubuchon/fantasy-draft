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
    fantasypros_api_key: str | None = None
    fantasypros_base_url: str = "https://api.fantasypros.com/public/v2/json"
    fantasypros_timeout_seconds: float = 10.0
    fantasypros_data_mode: str = "auto"
    dynastyprocess_url: str = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"
    cache_dir: Path = PROJECT_ROOT / "instance" / "cache"
    backup_dir: Path = PROJECT_ROOT / "instance" / "backups"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.6"
    openai_timeout_seconds: float = 5.0
    openai_reasoning_effort: str = "low"
    openai_prefetch_picks: int = 3
    survival_simulations: int = 2000

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
            fantasypros_api_key=os.getenv("FANTASYPROS_API_KEY") or None,
            fantasypros_base_url=os.getenv(
                "FANTASYPROS_BASE_URL", "https://api.fantasypros.com/public/v2/json"
            ).rstrip("/"),
            fantasypros_timeout_seconds=float(os.getenv("FANTASYPROS_TIMEOUT_SECONDS", "10")),
            fantasypros_data_mode=os.getenv("FANTASYPROS_DATA_MODE", "auto").lower(),
            dynastyprocess_url=os.getenv(
                "DYNASTYPROCESS_PLAYER_IDS_URL",
                "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv",
            ),
            cache_dir=Path(os.getenv("FANTASY_DRAFT_CACHE_DIR", PROJECT_ROOT / "instance" / "cache")),
            backup_dir=Path(os.getenv("FANTASY_DRAFT_BACKUP_DIR", PROJECT_ROOT / "instance" / "backups")),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6"),
            openai_timeout_seconds=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "5")),
            openai_reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "low"),
            openai_prefetch_picks=int(os.getenv("OPENAI_PREFETCH_PICKS", "3")),
            survival_simulations=int(os.getenv("SURVIVAL_SIMULATIONS", "2000")),
        )
