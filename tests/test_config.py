from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from fantasy_draft.config import LeagueConfig, dump_league_config, load_league_config_text


def test_sample_config_represents_all_draftable_slots(league_config):
    assert league_config.league.team_count == 8
    assert league_config.roster.draftable_size == 17
    assert league_config.draft.rounds == 17
    our_slot = league_config.team_by_id(league_config.draft.our_team_id).draft_slot
    assert 1 <= our_slot <= league_config.league.team_count
    assert league_config.scoring["passing"]["touchdowns"] == 5
    assert league_config.scoring["receiving"]["receptions"] == 1


def test_config_snapshot_round_trip(league_config):
    restored = load_league_config_text(dump_league_config(league_config))
    assert restored == league_config


def test_team_count_must_match_team_list(league_config):
    raw = league_config.model_dump()
    raw["league"]["team_count"] = 12
    with pytest.raises(ValidationError, match="team_count"):
        LeagueConfig.model_validate(raw)


def test_roster_rules_change_without_source_changes(league_config):
    raw = copy.deepcopy(league_config.model_dump())
    raw["draft"]["rounds"] = 16
    raw["roster"]["slots"] = [
        slot for slot in raw["roster"]["slots"] if slot["code"] != "K"
    ]
    raw["strategy"]["max_roster_counts"].pop("K")
    raw["strategy"]["position_target_rounds"].pop("K")
    changed = LeagueConfig.model_validate(raw)
    assert changed.roster.draftable_size == 16
    assert changed.draft.rounds == 16


def test_strategy_positions_and_target_rounds_are_validated(league_config):
    raw = league_config.model_dump()
    raw["strategy"]["position_target_rounds"]["K"] = 18
    with pytest.raises(ValidationError, match="cannot exceed draft rounds"):
        LeagueConfig.model_validate(raw)

    raw = league_config.model_dump()
    raw["strategy"]["max_roster_counts"]["PUNTER"] = 1
    with pytest.raises(ValidationError, match="unknown positions"):
        LeagueConfig.model_validate(raw)
