from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from fantasy_draft.config import configure_draft_session, load_league_config
from fantasy_draft.models import Player
from fantasy_draft.settings import AppSettings
from fantasy_draft.web import create_app


class SequencedResponses:
    def __init__(self):
        self.calls = 0
        self.fail_next = True

    def parse(self, **kwargs):
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            raise TimeoutError("simulated live timeout")
        packet = json.loads(kwargs["input"][1]["content"])
        ids = [item["player_id"] for item in packet["allowed_candidates"][:5]]
        output = kwargs["text_format"].model_validate({
            "recommendations": [
                {
                    "player_id": player_id,
                    "label": label,
                    "note": f"Short {label} case.",
                    "confidence": .8,
                }
                for player_id, label in zip(
                    ids, ["BEST", "SAFE", "UPSIDE", "VALUE", "STRATEGIC"]
                )
            ],
            "preferred_player_id": ids[0],
            "overall_confidence": .84,
            "reason": "Best combination of value and urgency.",
            "next_pick_strategy": "Watch the next value tier.",
        })
        return SimpleNamespace(
            output_parsed=output,
            model=kwargs["model"],
            reasoning=SimpleNamespace(effort="low"),
            status="completed",
            output=[],
        )


def test_live_draft_web_and_api_flow(tmp_path):
    root = Path(__file__).parents[1]
    settings = AppSettings(
        league_config_path=root / "config" / "league.yaml",
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        player_data_path=root / "data" / "players.csv",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        draft_state = app.state.draft_service.get_state(
            app.state.draft_service.current_draft_id()
        )
        draft_season = draft_state.season
        with app.state.session_factory.begin() as session:
            player = session.get(Player, "sample-josh-allen")
            player.birth_date = date(date.today().year - 30, 1, 1)
            player.draft_year = draft_season
        response = client.get("/")
        assert response.status_code == 200
        assert "On the clock" in response.text
        assert "Josh Allen" in response.text
        assert "AI FALLBACK ACTIVE" in response.text
        assert "deterministic quantitative recommendations" in response.text
        assert "data-instant-pick-form" in response.text
        assert "data-pick-form" not in response.text
        assert "/static/app.js?v=" in response.text
        assert "/static/style.css?v=" in response.text
        assert '<select name="position"' not in response.text
        assert 'class="position-filter-button active"' in response.text
        assert '>ALL</a>' in response.text
        assert '>QB</a>' in response.text
        assert '<th>Age</th>' in response.text
        assert 'title="NFL experience; R means rookie">Yr</th>' in response.text
        assert ">30</td>" in response.text
        assert ">R</td>" in response.text

        script = client.get("/static/app.js").text
        assert "Draft ${name} with the current pick?" not in script
        assert '[data-instant-pick-form]' in script
        assert 'button.textContent = "Saving…"' in script

        created = client.post("/api/picks", json={"player_id": "sample-bijan-robinson"})
        assert created.status_code == 201
        assert created.json()["overall_pick"] == 1

        state = client.get("/api/state").json()
        assert state["current_pick"]["overall"] == 2
        assert state["recommendation_is_fallback"] is True
        assert state["recommendation_fallback_reason"]
        assert state["picks"][0]["team_id"] == "team-1"
        assert "team-1" in state["team_needs"]
        assert len(state["team_remaining_slots"]["team-1"]) == 16
        assert all(player["id"] != "sample-bijan-robinson" for player in state["available_players"])
        josh = next(
            player for player in state["available_players"]
            if player["id"] == "sample-josh-allen"
        )
        assert josh["age"] == 30
        assert josh["experience"] == "R"
        assert josh["draft_year"] == draft_season

        duplicate = client.post("/api/picks", json={"player_id": "sample-bijan-robinson"})
        assert duplicate.status_code == 409

        corrected = client.put(
            "/api/picks/1", json={"player_id": "sample-jamarr-chase"}
        )
        assert corrected.status_code == 200
        assert client.get("/api/state").json()["picks"][0]["player"] == {
            "id": "sample-jamarr-chase",
            "name": "Ja'Marr Chase",
            "position": "WR",
        }

        undone = client.delete("/api/picks/last")
        assert undone.status_code == 200
        assert client.get("/api/state").json()["current_pick"]["overall"] == 1

        exported = client.get("/drafts/1/export.json")
        assert exported.status_code == 200
        assert exported.json()["draft"]["id"] == 1
        assert client.get("/drafts/1/export.csv").status_code == 200
        assert "RUN DRAFT READINESS CHECK" in client.get("/readiness").text
        readiness = client.post("/readiness")
        assert readiness.status_code == 200
        assert "DRAFT ENGINE" in readiness.text


def test_position_buttons_filter_and_pick_resets_to_all(tmp_path):
    root = Path(__file__).parents[1]
    settings = AppSettings(
        league_config_path=root / "config" / "league.yaml",
        database_url=f"sqlite:///{tmp_path / 'position-buttons.db'}",
        player_data_path=root / "data" / "players.csv",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        filtered = client.get("/?position=RB&sort=rank")
        assert filtered.status_code == 200
        assert 'position-filter-button pos-rb active' in filtered.text
        assert "Bijan Robinson" in filtered.text
        assert "Josh Allen" not in filtered.text

        state = app.state.draft_service.get_state(
            app.state.draft_service.current_draft_id()
        )
        drafted = client.post(
            "/picks",
            data={"player_id": state.available_players[0].id},
            follow_redirects=True,
        )
        assert drafted.status_code == 200
        assert 'class="position-filter-button active"' in drafted.text
        assert 'position-filter-button pos-rb active' not in drafted.text
        assert 'name="position" value=""' in drafted.text


def test_health_endpoint(tmp_path):
    root = Path(__file__).parents[1]
    settings = AppSettings(
        league_config_path=root / "config" / "league.yaml",
        database_url=f"sqlite:///{tmp_path / 'health.db'}",
        player_data_path=root / "data" / "players.csv",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_ai_runs_once_on_our_turn_then_allows_model_retry(tmp_path):
    root = Path(__file__).parents[1]
    settings = AppSettings(
        league_config_path=root / "config" / "league.yaml",
        database_url=f"sqlite:///{tmp_path / 'advisor-web.db'}",
        player_data_path=root / "data" / "players.csv",
        openai_api_key="test-value",
        survival_simulations=50,
    )
    app = create_app(settings)
    responses = SequencedResponses()
    app.state.openai_advisor._client = SimpleNamespace(responses=responses)

    with TestClient(app) as client:
        service = app.state.draft_service
        master = load_league_config(settings.league_config_path)
        config = configure_draft_session(
            master,
            team_count=master.league.team_count,
            our_draft_slot=2,
        )
        draft_id = service.create_draft(config, name="Advisor timing test")
        state = service.get_state(draft_id)
        picks_before_us = 1

        for index in range(picks_before_us):
            state = service.get_state(service.current_draft_id())
            response = client.post(
                "/picks",
                data={"player_id": state.available_players[0].id},
                follow_redirects=True,
            )
            assert response.status_code == 200
            assert responses.calls == (1 if index == picks_before_us - 1 else 0)

        state = service.get_state(service.current_draft_id())
        assert state.current_team_id == state.config.draft.our_team_id
        assert "AI FALLBACK ACTIVE" in response.text
        assert "Try AI again" in response.text

        refreshed = client.get("/")
        assert refreshed.status_code == 200
        assert responses.calls == 1
        assert "Previous gpt-5.6-terra attempt failed" in refreshed.text

        retried = client.post(
            "/advisor/retry",
            data={"model": "gpt-5.6-luna"},
            follow_redirects=True,
        )
        assert retried.status_code == 200
        assert responses.calls == 2
        assert "AI recommendation updated with gpt-5.6-luna" in retried.text
        assert "OpenAI gpt-5.6-luna" in retried.text

        client.get("/")
        assert responses.calls == 2
