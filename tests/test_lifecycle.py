from __future__ import annotations

import copy

from fantasy_draft.models import ImportRun, utc_now

from fantasy_draft.config import LeagueConfig, load_league_config_text


def test_create_setup_activate_archive_and_switch(service, league_config):
    draft_service, original_id, _ = service
    setup_id = draft_service.create_draft(
        league_config,
        name="Practice B",
        season=2030,
        draft_kind="practice",
        status="setup",
    )
    assert draft_service.current_draft_id() == setup_id
    assert draft_service.get_state(setup_id).draft_status == "setup"
    draft_service.activate_draft(setup_id)
    assert draft_service.get_state(setup_id).draft_status == "active"
    draft_service.archive_draft(setup_id)
    assert draft_service.get_state(setup_id).draft_status == "archived"
    draft_service.switch_draft(original_id)
    assert draft_service.current_draft_id() == original_id


def test_reset_archives_old_picks_and_starts_empty(service):
    draft_service, draft_id, _ = service
    draft_service.make_pick(draft_id, "player-1")
    replacement_id = draft_service.reset_draft(draft_id)
    old = draft_service.get_state(draft_id)
    new = draft_service.get_state(replacement_id)
    assert old.draft_status == "archived"
    assert [pick.player_id for pick in old.picks] == ["player-1"]
    assert new.draft_status == "active"
    assert new.picks == []
    assert old.config == new.config
    assert draft_service.current_draft_id() == replacement_id


def test_configuration_snapshot_isolation(service, league_config):
    draft_service, draft_id, _ = service
    changed_raw = copy.deepcopy(league_config.model_dump())
    changed_raw["league"]["name"] = "Changed Master"
    changed = LeagueConfig.model_validate(changed_raw)
    changed_id = draft_service.create_draft(changed, name="Changed", status="active")
    assert draft_service.get_state(draft_id).config.league.name != "Changed Master"
    assert draft_service.get_state(changed_id).config.league.name == "Changed Master"


def test_draft_sessions_are_isolated(service):
    draft_service, first_id, _ = service
    second_id = draft_service.create_draft(
        draft_service.get_state(first_id).config, name="Second", status="active"
    )
    draft_service.make_pick(first_id, "player-1")
    draft_service.make_pick(second_id, "player-2")
    assert [pick.player_id for pick in draft_service.get_state(first_id).picks] == ["player-1"]
    assert [pick.player_id for pick in draft_service.get_state(second_id).picks] == ["player-2"]


def test_new_draft_snapshots_only_matching_season_data(service, league_config):
    draft_service, _, session_factory = service
    with session_factory.begin() as session:
        old = ImportRun(provider="fantasypros", dataset="rankings", season=2029, status="success", data_mode="production", completed_at=utc_now())
        current = ImportRun(provider="fantasypros", dataset="rankings", season=2030, status="success", data_mode="production", completed_at=utc_now())
        session.add_all([old, current])
        session.flush()
        expected = current.id
    draft_id = draft_service.create_draft(league_config, season=2030)
    assert draft_service.get_state(draft_id).data_snapshot["fantasypros:rankings"] == expected
