from __future__ import annotations

import copy

import pytest

from fantasy_draft.config import LeagueConfig
from fantasy_draft.engine import (
    DraftRuleError,
    RosterPlayer,
    next_pick_for_team,
    pick_coordinates,
    picks_until,
    remaining_roster_slots,
    remaining_starting_needs,
    team_for_pick,
    teams_selecting_before,
    validate_roster,
)


def config_with_teams(base: LeagueConfig, count: int, our_slot: int = 1) -> LeagueConfig:
    raw = copy.deepcopy(base.model_dump())
    raw["league"]["team_count"] = count
    raw["teams"] = [
        {"id": f"team-{slot}", "name": f"Team {slot}", "draft_slot": slot}
        for slot in range(1, count + 1)
    ]
    raw["draft"]["our_team_id"] = f"team-{our_slot}"
    return LeagueConfig.model_validate(raw)


@pytest.mark.parametrize(
    ("overall", "round_number", "pick_in_round", "draft_slot"),
    [
        (1, 1, 1, 1),
        (12, 1, 12, 12),
        (13, 2, 1, 12),
        (24, 2, 12, 1),
        (25, 3, 1, 1),
        (36, 3, 12, 12),
    ],
)
def test_twelve_team_snake_coordinates(overall, round_number, pick_in_round, draft_slot):
    result = pick_coordinates(overall, 12)
    assert (result.round_number, result.pick_in_round, result.draft_slot) == (
        round_number,
        pick_in_round,
        draft_slot,
    )


def test_snake_team_order_reverses_each_round(league_config):
    config = config_with_teams(league_config, 12)
    assert [team_for_pick(config, pick) for pick in range(1, 13)] == [
        f"team-{slot}" for slot in range(1, 13)
    ]
    assert [team_for_pick(config, pick) for pick in range(13, 25)] == [
        f"team-{slot}" for slot in range(12, 0, -1)
    ]
    assert [team_for_pick(config, pick) for pick in range(25, 37)] == [
        f"team-{slot}" for slot in range(1, 13)
    ]


def test_next_pick_and_distance_for_our_team(league_config):
    config = config_with_teams(league_config, 12, our_slot=4)
    assert next_pick_for_team(config, "team-4", 1) == 4
    assert picks_until(4, 1) == 3
    assert next_pick_for_team(config, "team-4", 4) == 4
    assert picks_until(4, 4) == 0
    assert next_pick_for_team(config, "team-4", 5) == 21
    assert picks_until(21, 5) == 16
    between = teams_selecting_before(config, 5, 21)
    assert len(between) == 16
    assert between[0] == "team-5"
    assert between[-1] == "team-5"


def test_pick_coordinate_rejects_invalid_input():
    with pytest.raises(DraftRuleError):
        pick_coordinates(0, 12)


def test_roster_assignment_honors_configured_flex_and_bench(league_config):
    players = [
        RosterPlayer("rb-1", frozenset({"RB"})),
        RosterPlayer("rb-2", frozenset({"RB"})),
        RosterPlayer("rb-3", frozenset({"RB"})),
        RosterPlayer("wr-1", frozenset({"WR"})),
        RosterPlayer("wr-2", frozenset({"WR"})),
        RosterPlayer("wr-3", frozenset({"WR"})),
        RosterPlayer("wr-4", frozenset({"WR"})),
    ]
    assignment = validate_roster(players, league_config)
    assert len(assignment) == 7
    assert any(slot.startswith("FLEX") for slot in assignment.values())
    needs = remaining_starting_needs(assignment, league_config)
    assert needs.count("RB") == 0
    assert needs.count("WR") == 0
    assert "QB" in needs
    assert len(remaining_roster_slots(assignment, league_config)) == 10


def test_roster_rejects_more_players_than_capacity(league_config):
    players = [RosterPlayer(str(i), frozenset({"QB"})) for i in range(9)]
    with pytest.raises(DraftRuleError, match="remaining roster"):
        validate_roster(players, league_config)
