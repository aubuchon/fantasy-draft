from __future__ import annotations

from fantasy_draft.evaluation import (
    BaselinePlayerEvaluator,
    OfflineStrategicAdvisor,
    ResilientStrategicAdvisor,
    calculate_replacement_profile,
    calculate_tiers,
)
from fantasy_draft.config import LeagueConfig, configure_draft_session
from fantasy_draft.engine import next_pick_for_team
from fantasy_draft.models import Player


class FailingAdvisor:
    def recommend(self, state, evaluated):
        raise TimeoutError("model timed out")


def test_evaluator_uses_following_pick_for_survival(service):
    draft_service, draft_id, _ = service
    state = draft_service.get_state(draft_id)
    evaluated = BaselinePlayerEvaluator().evaluate(state, state.available_players)
    first = next(item for item in evaluated if item.player.id == "player-1")
    assert state.next_user_pick == next_pick_for_team(
        state.config, state.config.draft.our_team_id, state.current_pick.overall
    )
    assert first.survival_probability is not None
    assert 0 <= first.survival_probability <= 1


def test_failed_advisor_falls_back_without_affecting_state(service):
    draft_service, draft_id, _ = service
    state = draft_service.get_state(draft_id)
    evaluated = BaselinePlayerEvaluator().evaluate(state, state.available_players)
    advisor = ResilientStrategicAdvisor(FailingAdvisor(), OfflineStrategicAdvisor())
    result = advisor.recommend(state, evaluated)
    assert result.source == "AI unavailable — quantitative fallback"
    assert result.preferred is not None
    assert result.is_fallback is True
    assert result.fallback_reason == "AI request failed (TimeoutError)."
    assert draft_service.get_state(draft_id).picks == []


def test_deterministic_advisor_excludes_duplicate_player_identities(service):
    draft_service, draft_id, session_factory = service
    with session_factory.begin() as session:
        session.add(Player(
            id="duplicate-player-1",
            name="Player 1",
            nfl_team="OLD",
            primary_position="RB",
            eligible_positions=["RB"],
            overall_rank=50,
            active=True,
        ))
    state = draft_service.get_state(draft_id)
    evaluated = BaselinePlayerEvaluator(simulations=50).evaluate(
        state, state.available_players
    )
    result = OfflineStrategicAdvisor().recommend(state, evaluated)
    recommendation_ids = {item.player.id for item in result.recommendations}
    assert "player-1" not in recommendation_ids
    assert "duplicate-player-1" not in recommendation_ids


def test_replacement_level_reacts_to_team_count_and_starters(league_config):
    projections = {"RB": [300 - index * 5 for index in range(80)], "WR": [290 - index * 4 for index in range(100)], "QB": [350 - index * 8 for index in range(40)], "TE": [240 - index * 7 for index in range(40)]}
    eight = calculate_replacement_profile(league_config, projections)
    twelve_config = configure_draft_session(league_config, team_count=12, our_draft_slot=1)
    twelve = calculate_replacement_profile(twelve_config, projections)
    assert twelve.demand["RB"] > eight.demand["RB"]
    assert twelve.levels["RB"] < eight.levels["RB"]

    raw = league_config.model_dump()
    next(slot for slot in raw["roster"]["slots"] if slot["code"] == "RB")["count"] = 3
    raw["draft"]["rounds"] = min(raw["draft"]["rounds"], sum(slot["count"] for slot in raw["roster"]["slots"] if slot["draftable"]))
    extra_rb = calculate_replacement_profile(LeagueConfig.model_validate(raw), projections)
    assert extra_rb.demand["RB"] > eight.demand["RB"]


def test_flex_eligibility_changes_replacement_demand(league_config):
    projections = {"RB": [400 - index * 5 for index in range(80)], "WR": [290 - index * 4 for index in range(100)]}
    rb_wr = calculate_replacement_profile(league_config, projections)
    raw = league_config.model_dump()
    next(slot for slot in raw["roster"]["slots"] if slot["code"] == "FLEX")["eligible_positions"] = ["WR"]
    wr_only = calculate_replacement_profile(LeagueConfig.model_validate(raw), projections)
    assert wr_only.demand["WR"] > rb_wr.demand["WR"]
    assert wr_only.demand["RB"] < rb_wr.demand["RB"]


def test_calculated_tiers_expose_large_projection_cliff():
    tiers, cliffs = calculate_tiers({"TE": [("te1", 225), ("te2", 221), ("te3", 217), ("te4", 181)]})
    assert cliffs["te3"] == 36
    assert tiers["te4"] == tiers["te3"] + 1


def test_survival_simulation_is_seeded_and_explainable(service):
    draft_service, draft_id, _ = service
    state = draft_service.get_state(draft_id)
    first = BaselinePlayerEvaluator(simulations=300).evaluate(state, state.available_players)
    second = BaselinePlayerEvaluator(simulations=300).evaluate(state, state.available_players)
    assert [item.survival_probability for item in first] == [item.survival_probability for item in second]
    assert all(item.simulations == 300 for item in first)
