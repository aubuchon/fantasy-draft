from __future__ import annotations

from pathlib import Path

import pytest

from fantasy_draft.config import LeagueConfig, load_league_config
from fantasy_draft.database import Base, create_database_engine, create_session_factory
from fantasy_draft.models import Player
from fantasy_draft.services import DraftService


@pytest.fixture
def league_config() -> LeagueConfig:
    return load_league_config(Path(__file__).parents[1] / "config" / "league.yaml")


@pytest.fixture
def service(tmp_path, league_config):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    with session_factory.begin() as session:
        for index, position in enumerate(["RB", "WR", "QB", "TE", "K", "DEF"], start=1):
            session.add(
                Player(
                    id=f"player-{index}",
                    name=f"Player {index}",
                    primary_position=position,
                    eligible_positions=[position],
                    overall_rank=index,
                    adp=float(index),
                    active=True,
                )
            )
    draft_service = DraftService(session_factory)
    draft_id = draft_service.create_draft(league_config)
    yield draft_service, draft_id, session_factory
    engine.dispose()

