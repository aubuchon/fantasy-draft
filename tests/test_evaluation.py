from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

from fantasy_draft.evaluation import (
    BaselinePlayerEvaluator,
    EvaluatedPlayer,
    OfflineStrategicAdvisor,
    PlayerMetrics,
    ResilientStrategicAdvisor,
    calculate_preference_adjustment,
    calculate_replacement_profile,
    calculate_tiers,
    select_advisor_candidates,
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


def test_replacement_level_accounts_for_players_already_drafted(league_config):
    projections = {"QB": [400 - index * 10 for index in range(30)]}
    before = calculate_replacement_profile(league_config, projections)
    after = calculate_replacement_profile(
        league_config, projections, Counter({"QB": 9})
    )
    assert after.levels["QB"] > before.levels["QB"]


def test_advisor_allowlist_expands_to_limit_with_position_diversity(service):
    draft_service, draft_id, _ = service
    original = draft_service.get_state(draft_id)
    our_team = original.config.draft.our_team_id
    qb_roster = [
        SimpleNamespace(
            team_id=our_team,
            player=Player(
                id=f"rostered-qb-{index}", name=f"Rostered QB {index}",
                primary_position="QB", eligible_positions=["QB"], active=True,
            ),
        )
        for index in range(2)
    ]
    state = SimpleNamespace(
        config=original.config,
        team_needs={our_team: ["TE", "K", "DEF"]},
        picks=qb_roster,
        current_pick=SimpleNamespace(round_number=12),
    )

    def item(player_id, name, position, score):
        return EvaluatedPlayer(
            player=Player(
                id=player_id, name=name, primary_position=position,
                eligible_positions=[position], active=True,
            ),
            projected_points=score, vor=score, replacement_level=0,
            ecr=score, adp=score, provider_tier=1, our_tier=1,
            tier_cliff=0, scarcity=0, survival_probability=.5,
            cost_of_waiting=0, roster_fit=0, upside_adjustment=0,
            risk_adjustment=0, preference_adjustment=0,
            quantitative_score=score, simulations=10,
        )

    evaluated = [
        item(f"qb-{index}", f"Quarterback {index}", "QB", 200 - index)
        for index in range(10)
    ] + [
        item(f"te-{index}", f"Tight End {index}", "TE", 190 - index)
        for index in range(10)
    ] + [
        item(f"rb-{index}", f"Running Back {index}", "RB", 180 - index)
        for index in range(10)
    ] + [
        item(f"wr-{index}", f"Wide Receiver {index}", "WR", 170 - index)
        for index in range(10)
    ] + [
        item(f"k-{index}", f"Kicker {index}", "K", 90 - index)
        for index in range(5)
    ] + [
        item(f"def-{index}", f"Defense {index}", "DEF", 80 - index)
        for index in range(5)
    ]
    selected = select_advisor_candidates(state, evaluated, limit=30)
    counts = Counter(candidate.player.primary_position for candidate in selected)
    assert len(selected) == 30
    assert counts["QB"] == 6
    assert counts["TE"] == 9
    assert counts["RB"] == 9
    assert counts["WR"] == 4
    assert counts["K"] == 1
    assert counts["DEF"] == 1


def test_rookie_bias_grows_late_and_team_bias_stays_small(league_config):
    raw = league_config.model_dump()
    raw["strategy"]["rookie_late_round_bonus"] = 4.0
    raw["strategy"]["preferred_nfl_team_bonuses"] = {"CHI": 1.0}
    config = LeagueConfig.model_validate(raw)
    rookie = Player(
        id="rookie", name="High-upside Rookie", nfl_team="CHI",
        primary_position="WR", eligible_positions=["WR"],
        draft_year=2026, upside=.9, active=True,
    )
    metrics = PlayerMetrics(180, 100, 105, 2, 8)
    early = calculate_preference_adjustment(
        config, rookie, metrics, season=2026, draft_progress=.2, current_round=3
    )
    late = calculate_preference_adjustment(
        config, rookie, metrics, season=2026, draft_progress=.8, current_round=14
    )
    assert late > early
    assert early >= 1.0
    assert late <= 5.0


def test_position_target_round_is_soft_but_penalizes_early_selection(league_config):
    kicker = Player(
        id="kicker", name="Kicker", primary_position="K",
        eligible_positions=["K"], active=True,
    )
    metrics = PlayerMetrics(130, 150, 155, 1, 8)
    early = calculate_preference_adjustment(
        league_config, kicker, metrics,
        season=2026, draft_progress=.7, current_round=12,
    )
    on_target = calculate_preference_adjustment(
        league_config, kicker, metrics,
        season=2026, draft_progress=1, current_round=17,
    )
    assert early == -40
    assert on_target == 0


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
