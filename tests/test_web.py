from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from fantasy_draft.settings import AppSettings
from fantasy_draft.web import create_app


def test_live_draft_web_and_api_flow(tmp_path):
    root = Path(__file__).parents[1]
    settings = AppSettings(
        league_config_path=root / "config" / "league.yaml",
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        player_data_path=root / "data" / "players.csv",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "On the clock" in response.text
        assert "Josh Allen" in response.text
        assert "AI FALLBACK ACTIVE" in response.text
        assert "deterministic quantitative recommendations" in response.text

        script = client.get("/static/app.js").text
        assert "Draft ${name} with the current pick?" not in script
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


def test_health_endpoint(tmp_path):
    root = Path(__file__).parents[1]
    settings = AppSettings(
        league_config_path=root / "config" / "league.yaml",
        database_url=f"sqlite:///{tmp_path / 'health.db'}",
        player_data_path=root / "data" / "players.csv",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
