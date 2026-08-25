from __future__ import annotations

import pytest
from sqlalchemy import func, select

from fantasy_draft.engine import next_pick_for_team, pick_coordinates, team_for_pick
from fantasy_draft.config import LeagueConfig, StrategyPreferences
from fantasy_draft.models import DraftPick, Player
from fantasy_draft.services import DraftConflictError, DraftService


def test_pick_updates_availability_and_correct_team(service):
    draft_service, draft_id, _ = service
    assert draft_service.make_pick(draft_id, "player-1") == 1
    state = draft_service.get_state(draft_id)
    assert state.picks[0].team_id == "team-1"
    assert state.picks[0].player.name == "Player 1"
    assert "player-1" not in {player.id for player in state.available_players}
    assert state.current_pick.overall == 2
    assert state.current_team_id == "team-2"
    assert state.team_needs["team-1"].count("RB") == 1
    expected_next = next_pick_for_team(
        state.config, state.config.draft.our_team_id, state.current_pick.overall
    )
    assert state.next_user_pick == expected_next
    assert state.picks_until_user_pick == expected_next - state.current_pick.overall
    assert len(state.team_remaining_slots["team-1"]) == 16


def test_duplicate_player_is_impossible(service):
    draft_service, draft_id, _ = service
    draft_service.make_pick(draft_id, "player-1")
    with pytest.raises(DraftConflictError, match="already been drafted"):
        draft_service.make_pick(draft_id, "player-1")


def test_undo_restores_player_and_pick_position(service):
    draft_service, draft_id, _ = service
    draft_service.make_pick(draft_id, "player-1")
    undone = draft_service.undo_last_pick(draft_id)
    assert undone.overall_pick == 1
    state = draft_service.get_state(draft_id)
    assert state.picks == []
    assert "player-1" in {player.id for player in state.available_players}
    assert state.current_pick.overall == 1


def test_correct_pick_preserves_draft_sequence(service):
    draft_service, draft_id, _ = service
    draft_service.make_pick(draft_id, "player-1")
    draft_service.make_pick(draft_id, "player-2")
    draft_service.correct_pick(draft_id, 1, "player-3")
    state = draft_service.get_state(draft_id)
    assert [pick.player_id for pick in state.picks] == ["player-3", "player-2"]
    assert state.current_pick.overall == 3
    assert "player-1" in {player.id for player in state.available_players}


def test_correct_pick_cannot_duplicate_later_player(service):
    draft_service, draft_id, _ = service
    draft_service.make_pick(draft_id, "player-1")
    draft_service.make_pick(draft_id, "player-2")
    with pytest.raises(DraftConflictError, match="already been drafted"):
        draft_service.correct_pick(draft_id, 1, "player-2")


def test_reload_reproduces_identical_state(service):
    draft_service, draft_id, session_factory = service
    draft_service.make_pick(draft_id, "player-1")
    draft_service.make_pick(draft_id, "player-2")
    reloaded_service = DraftService(session_factory)
    reloaded = reloaded_service.get_state(draft_id)
    assert [(pick.overall_pick, pick.team_id, pick.player_id) for pick in reloaded.picks] == [
        (1, "team-1", "player-1"),
        (2, "team-2", "player-2"),
    ]
    assert reloaded.current_pick.overall == 3


def test_strategy_preferences_can_update_without_changing_picks(service):
    draft_service, draft_id, _ = service
    draft_service.make_pick(draft_id, "player-1")
    draft_service.update_strategy_preferences(
        draft_id,
        StrategyPreferences(
            rookie_late_round_bonus=4,
            preferred_nfl_team_bonuses={"chi": 1},
        ),
    )
    state = draft_service.get_state(draft_id)
    assert [pick.player_id for pick in state.picks] == ["player-1"]
    assert state.config.strategy.rookie_late_round_bonus == 4
    assert state.config.strategy.preferred_nfl_team_bonuses == {"CHI": 1}


def test_failed_pick_is_atomic(service):
    draft_service, draft_id, session_factory = service
    draft_service.make_pick(draft_id, "player-1")
    with pytest.raises(DraftConflictError):
        draft_service.make_pick(draft_id, "player-1")
    with session_factory() as session:
        count = session.scalar(
            select(func.count(DraftPick.id)).where(DraftPick.draft_id == draft_id)
        )
    assert count == 1


def _config_with_user_in_slot_one(config) -> LeagueConfig:
    raw = config.model_dump()
    raw["draft"]["our_team_id"] = "team-1"
    return LeagueConfig.model_validate(raw)


@pytest.mark.parametrize(
    ("position", "existing_player_id"),
    [("K", "player-5"), ("DEF", "player-6")],
)
def test_user_position_maximum_prevents_second_special_team(
    service, position, existing_player_id
):
    draft_service, original_draft_id, session_factory = service
    config = _config_with_user_in_slot_one(
        draft_service.get_state(original_draft_id).config
    )
    draft_id = draft_service.create_draft(config, name="Position max test")
    with session_factory.begin() as session:
        session.add(Player(
            id="second-special", name="Second Special Team", primary_position=position,
            eligible_positions=[position], active=True,
        ))
        for index in range(14):
            session.add(Player(
                id=f"limit-filler-{index}", name=f"Limit Filler {index}",
                primary_position="RB", eligible_positions=["RB"], active=True,
            ))
        selected_ids = [existing_player_id] + [f"limit-filler-{index}" for index in range(14)]
        for overall, player_id in enumerate(selected_ids, start=1):
            coordinates = pick_coordinates(overall, config.league.team_count)
            session.add(DraftPick(
                draft_id=draft_id,
                overall_pick=overall,
                round_number=coordinates.round_number,
                pick_in_round=coordinates.pick_in_round,
                team_id=team_for_pick(config, overall),
                player_id=player_id,
            ))
    with pytest.raises(DraftConflictError, match=f"limits our roster to 1 {position}"):
        draft_service.make_pick(draft_id, "second-special")


def test_defense_round_sixteen_and_kicker_round_seventeen_are_legal(service):
    draft_service, original_draft_id, session_factory = service
    config = _config_with_user_in_slot_one(
        draft_service.get_state(original_draft_id).config
    )
    draft_id = draft_service.create_draft(config, name="Late K and DEF test")
    position_plan = [
        "QB", "WR", "WR", "WR", "RB", "RB", "TE", "RB",
        "WR", "RB", "QB", "WR", "RB", "TE", "WR", "DEF",
    ]
    team_pick_counts = {team.id: 0 for team in config.teams}
    user_pick_ids: list[str] = []
    with session_factory.begin() as session:
        for overall in range(1, 129):
            team_id = team_for_pick(config, overall)
            position = position_plan[team_pick_counts[team_id]]
            team_pick_counts[team_id] += 1
            player_id = f"late-plan-{overall}"
            session.add(Player(
                id=player_id, name=f"Late Plan {overall}",
                primary_position=position, eligible_positions=[position], active=True,
            ))
            coordinates = pick_coordinates(overall, config.league.team_count)
            session.add(DraftPick(
                draft_id=draft_id,
                overall_pick=overall,
                round_number=coordinates.round_number,
                pick_in_round=coordinates.pick_in_round,
                team_id=team_id,
                player_id=player_id,
            ))
            if team_id == "team-1":
                user_pick_ids.append(player_id)
        session.add(Player(
            id="last-round-kicker", name="Last Round Kicker", primary_position="K",
            eligible_positions=["K"], active=True,
        ))
    assert len(user_pick_ids) == 16
    assert draft_service.make_pick(draft_id, "last-round-kicker") == 129
    state = draft_service.get_state(draft_id)
    assert state.picks[-1].round_number == 17
    assert state.picks[-1].player.primary_position == "K"
    assert next(
        pick for pick in state.picks
        if pick.team_id == "team-1" and pick.round_number == 16
    ).player.primary_position == "DEF"
