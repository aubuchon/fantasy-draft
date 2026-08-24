from __future__ import annotations

import copy

import httpx
import pytest
from sqlalchemy import func, select

from fantasy_draft.data_import import DataImportService, detect_data_mode
from fantasy_draft.models import Player, PlayerExternalId, PlayerProjection, PlayerRanking
from fantasy_draft.providers import FantasyProsProvider, ProviderError, ProviderPayload
from fantasy_draft.scoring import score_projection


def player_payload(records):
    return ProviderPayload("fantasypros", "players", records, {"players": records, "test": True}, {})


def test_provider_uses_header_and_documented_players_parameters():
    seen = {}
    def handler(request):
        seen["header"] = request.headers.get("x-api-key")
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"players": []})
    provider = FantasyProsProvider(
        "test-secret", base_url="https://example.test", client=httpx.Client(
            transport=httpx.MockTransport(handler)
        )
    )
    provider.get_players()
    assert seen["header"] == "test-secret"
    assert seen["query"]["external_ids"] == "yahoo:espn:cbs:nfl"


def test_provider_requests_preseason_projections_and_ranking_dispersion():
    requests = []
    def handler(request):
        requests.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={"players": []})
    provider = FantasyProsProvider(
        "test-secret", base_url="https://example.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.get_rankings(2030, "PPR", "DRAFT")
    provider.get_projections(2030, ["QB", "DEF"])
    assert requests[0][1] == {
        "position": "ALL", "type": "DRAFT", "scoring": "PPR",
        "week": "0", "range": "true", "rankstats": "true",
    }
    assert requests[1][1]["week"] == "0"
    assert requests[2][1]["position"] == "DST"


def test_provider_failure_never_includes_key():
    def handler(_request):
        raise httpx.ReadTimeout("timed out")
    provider = FantasyProsProvider(
        "never-show-me", base_url="https://example.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError) as error:
        provider.get_players()
    assert "never-show-me" not in str(error.value)


def test_player_import_uses_canonical_ids_and_prevents_duplicates(service, tmp_path):
    _, _, session_factory = service
    importer = DataImportService(session_factory, tmp_path, data_mode="test")
    record = {
        "player_id": 123, "player_name": "New Runner", "position_id": "RB",
        "team_id": "CHI", "player_yahoo_id": "999", "rank_ecr": 25,
    }
    first = importer.import_players_payload(player_payload([record]))
    second = importer.import_players_payload(player_payload([record]))
    with session_factory() as session:
        external = session.scalar(select(PlayerExternalId).where(
            PlayerExternalId.provider == "fantasypros", PlayerExternalId.external_id == "123"
        ))
        assert external.player_id.startswith("player-")
        assert session.scalar(select(func.count(PlayerExternalId.id)).where(
            PlayerExternalId.provider == "fantasypros", PlayerExternalId.external_id == "123"
        )) == 1
        assert session.get(Player, external.player_id).external_ids["yahoo"] == "999"
    assert first != second


def test_ambiguous_name_is_sent_to_review(service, tmp_path):
    _, _, session_factory = service
    with session_factory.begin() as session:
        session.add(Player(
            id="duplicate-runner", name="Player 1 Jr.", nfl_team=None,
            primary_position="RB", eligible_positions=["RB"], active=True,
        ))
    importer = DataImportService(session_factory, tmp_path)
    run_id = importer.import_players_payload(player_payload([{
        "player_id": 500, "player_name": "Player 1", "position_id": "RB", "team_id": None,
    }]))
    status = importer.status()
    assert status["unmatched"] == 1


def test_rankings_and_projections_match_by_fantasypros_id(service, tmp_path):
    _, _, session_factory = service
    importer = DataImportService(session_factory, tmp_path, data_mode="sample")
    importer.import_players_payload(player_payload([{
        "player_id": 123, "player_name": "New Runner", "position_id": "RB", "team_id": "CHI",
    }]))
    ranking = ProviderPayload(
        "fantasypros", "rankings",
        [{"player_id": 123, "player_name": "New Runner", "rank_ecr": 4, "pos_rank": "RB2", "tier": 1, "rank_std": 2.5}],
        {"players": [], "test": True},
        {"season": 2030, "scoring": "PPR", "ranking_type": "DRAFT"},
    )
    importer.import_rankings_payload(ranking)
    projection = ProviderPayload(
        "fantasypros", "projections",
        [{"fpid": 123, "name": "New Runner", "stats": {"rush_yds": 1000, "rush_tds": 8, "rec_rec": 50}}],
        {"players": [], "test": True},
        {"season": 2030, "projection_type": "preseason"},
    )
    importer.import_projections_payload(projection)
    with session_factory() as session:
        assert session.scalar(select(func.count(PlayerRanking.id))) == 1
        assert session.scalar(select(func.count(PlayerProjection.id))) == 1


def test_league_scoring_uses_raw_stats_and_bonuses(league_config):
    result = score_projection({
        "pass_yds": 4000, "pass_tds": 30, "pass_ints": 10, "pass_yds_400": 2,
        "rush_yds": 500, "rush_tds": 5, "rush_yds_100": 1,
    }, "QB", league_config)
    assert result.points == 386
    changed_raw = copy.deepcopy(league_config.model_dump())
    changed_raw["scoring"]["passing"]["touchdowns"] = 4
    changed = type(league_config).model_validate(changed_raw)
    assert score_projection({"pass_tds": 30}, "QB", changed).points == 120


def test_sample_data_detection_is_explicit():
    assert detect_data_mode({"test": True}, "auto") == "sample"
    assert detect_data_mode({}, "production") == "production"
    assert detect_data_mode({}, "auto") == "unknown"


def test_failed_refresh_is_audited_and_preserves_cached_players(service, tmp_path):
    _, _, session_factory = service
    importer = DataImportService(session_factory, tmp_path)

    class FailedProvider:
        def get_players(self): raise ProviderError("players unavailable")
        def get_rankings(self, *args): raise ProviderError("rankings unavailable")
        def get_projections(self, *args): raise ProviderError("projections unavailable")

    class FailedCrosswalk:
        def fetch_csv(self): raise ProviderError("crosswalk unavailable")

    before = importer.status()["players"]
    result = importer.refresh_all(
        FailedProvider(), FailedCrosswalk(), season=2030, scoring="PPR", positions=["RB"]
    )
    assert len(result.warnings) == 5
    assert importer.status()["players"] == before
    assert all(run.status == "failed" for run in importer.status()["latest"].values())
