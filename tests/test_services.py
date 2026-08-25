from __future__ import annotations

import pytest
from sqlalchemy import func, select

from fantasy_draft.engine import next_pick_for_team
from fantasy_draft.models import DraftPick
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
