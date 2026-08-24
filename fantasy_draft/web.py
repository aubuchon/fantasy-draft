from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select

from fantasy_draft.config import load_league_config
from fantasy_draft.database import Base, create_database_engine, create_session_factory
from fantasy_draft.engine import DraftRuleError
from fantasy_draft.evaluation import (
    BaselinePlayerEvaluator,
    OfflineStrategicAdvisor,
    ResilientStrategicAdvisor,
)
from fantasy_draft.models import Player
from fantasy_draft.players import seed_players_if_empty
from fantasy_draft.services import DraftNotFoundError, DraftService, PlayerNotFoundError
from fantasy_draft.settings import AppSettings, PROJECT_ROOT


class PickRequest(BaseModel):
    player_id: str


class CorrectionRequest(BaseModel):
    player_id: str


def _redirect(message: str = "", error: str = "") -> RedirectResponse:
    query = urlencode({key: value for key, value in {"message": message, "error": error}.items() if value})
    return RedirectResponse(url=f"/{'?' + query if query else ''}", status_code=303)


def _state_payload(state, recommendations) -> dict:
    return {
        "draft_id": state.draft_id,
        "league": state.config.league.model_dump(),
        "current_pick": (
            {
                **state.current_pick.__dict__,
                "team_id": state.current_team_id,
            }
            if state.current_pick
            else None
        ),
        "our_next_pick": state.next_user_pick,
        "picks_until_our_pick": state.picks_until_user_pick,
        "teams_before_our_pick": state.teams_before_user_pick,
        "team_needs": state.team_needs,
        "team_remaining_slots": state.team_remaining_slots,
        "picks": [
            {
                "overall": pick.overall_pick,
                "round": pick.round_number,
                "pick_in_round": pick.pick_in_round,
                "team_id": pick.team_id,
                "player": {
                    "id": pick.player.id,
                    "name": pick.player.name,
                    "position": pick.player.primary_position,
                },
            }
            for pick in state.picks
        ],
        "available_players": [
            {
                "id": player.id,
                "name": player.name,
                "nfl_team": player.nfl_team,
                "position": player.primary_position,
                "overall_rank": player.overall_rank,
                "adp": player.adp,
                "tier": player.tier,
                "projected_points": player.projected_points,
            }
            for player in state.available_players
        ],
        "recommendations": [
            {
                "category": item.category,
                "player_id": item.player.id,
                "player": item.player.name,
                "position": item.player.primary_position,
                "explanation": item.explanation,
                "confidence": item.confidence,
            }
            for item in recommendations.recommendations
        ],
        "recommendation_source": recommendations.source,
    }


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or AppSettings.from_environment()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    service = DraftService(session_factory)
    evaluator = BaselinePlayerEvaluator()
    offline_advisor = OfflineStrategicAdvisor()
    advisor = ResilientStrategicAdvisor(offline_advisor, offline_advisor)
    templates = Jinja2Templates(directory=PROJECT_ROOT / "templates")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        Base.metadata.create_all(engine)
        config = load_league_config(settings.league_config_path)
        with session_factory.begin() as session:
            seed_players_if_empty(session, settings.player_data_path)
        service.get_or_create_active_draft(config)
        yield
        engine.dispose()

    app = FastAPI(title="Fantasy Draft AI", version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "static"), name="static")
    app.state.draft_service = service
    app.state.session_factory = session_factory

    def active_draft_id() -> int:
        config = load_league_config(settings.league_config_path)
        return service.get_or_create_active_draft(config)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        search: str = Query(default=""),
        position: str = Query(default=""),
        sort: str = Query(default="rank"),
        message: str = Query(default=""),
        error: str = Query(default=""),
    ):
        draft_id = active_draft_id()
        state = service.get_state(draft_id)
        visible_state = service.get_state(draft_id, search=search, position=position, sort=sort)
        evaluated = evaluator.evaluate(state, state.available_players)
        recommendations = advisor.recommend(state, evaluated)
        teams = sorted(state.config.teams, key=lambda team: team.draft_slot)
        pick_lookup = {(pick.round_number, pick.team_id): pick for pick in state.picks}
        rosters = {
            team.id: [pick for pick in state.picks if pick.team_id == team.id]
            for team in teams
        }
        team_names = {team.id: team.name for team in teams}
        current_team = team_names.get(state.current_team_id or "", "Draft complete")
        positions = sorted({
            eligible
            for slot in state.config.roster.slots
            if slot.draftable
            for eligible in slot.eligible_positions
        })
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "state": state,
                "available": visible_state.available_players,
                "recommendations": recommendations,
                "teams": teams,
                "team_names": team_names,
                "current_team": current_team,
                "teams_before": list(dict.fromkeys(state.teams_before_user_pick)),
                "positions": positions,
                "pick_lookup": pick_lookup,
                "rosters": rosters,
                "search": search,
                "position": position,
                "sort": sort,
                "message": message,
                "error": error,
            },
        )

    @app.post("/picks")
    def enter_pick(player_id: str = Form()):
        try:
            overall = service.make_pick(active_draft_id(), player_id)
            return _redirect(message=f"Saved pick {overall}.")
        except (DraftRuleError, PlayerNotFoundError, DraftNotFoundError) as exc:
            return _redirect(error=str(exc))

    @app.post("/picks/undo")
    def undo_pick():
        try:
            pick = service.undo_last_pick(active_draft_id())
            return _redirect(message=f"Undid pick {pick.overall_pick}: {pick.player.name}.")
        except (DraftRuleError, DraftNotFoundError) as exc:
            return _redirect(error=str(exc))

    @app.post("/picks/{overall_pick}/correct")
    def correct_pick(overall_pick: int, player_id: str = Form()):
        try:
            service.correct_pick(active_draft_id(), overall_pick, player_id)
            return _redirect(message=f"Corrected pick {overall_pick}.")
        except (DraftRuleError, PlayerNotFoundError, DraftNotFoundError) as exc:
            return _redirect(error=str(exc))

    @app.get("/api/state")
    def api_state():
        state = service.get_state(active_draft_id())
        recommendations = advisor.recommend(
            state, evaluator.evaluate(state, state.available_players)
        )
        return _state_payload(state, recommendations)

    @app.post("/api/picks", status_code=201)
    def api_enter_pick(payload: PickRequest):
        try:
            overall = service.make_pick(active_draft_id(), payload.player_id)
            return {"overall_pick": overall, "status": "saved"}
        except (DraftRuleError, PlayerNotFoundError, DraftNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/picks/last")
    def api_undo_pick():
        try:
            pick = service.undo_last_pick(active_draft_id())
            return {"overall_pick": pick.overall_pick, "player_id": pick.player_id, "status": "undone"}
        except (DraftRuleError, DraftNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/api/picks/{overall_pick}")
    def api_correct_pick(overall_pick: int, payload: CorrectionRequest):
        try:
            service.correct_pick(active_draft_id(), overall_pick, payload.player_id)
            return {"overall_pick": overall_pick, "player_id": payload.player_id, "status": "corrected"}
        except (DraftRuleError, PlayerNotFoundError, DraftNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/health")
    def health():
        with session_factory() as session:
            session.scalar(select(Player.id).limit(1))
        return {"status": "ok"}

    return app


app = create_app()
