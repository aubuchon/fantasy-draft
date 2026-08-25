from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
import hashlib
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select

from fantasy_draft.config import configure_draft_session, load_league_config
from fantasy_draft.database import create_database_engine, create_session_factory
from fantasy_draft.engine import DraftRuleError
from fantasy_draft.data_import import DataImportService
from fantasy_draft.evaluation import (
    BaselinePlayerEvaluator,
    OfflineStrategicAdvisor,
    ResilientStrategicAdvisor,
)
from fantasy_draft.models import Player
from fantasy_draft.llm import OpenAIStrategicAdvisor, classify_openai_failure
from fantasy_draft.operations import DatabaseBackupService, DraftExporter
from fantasy_draft.readiness import ReadinessService
from fantasy_draft.migrations import run_migrations
from fantasy_draft.players import age_on, experience_label, seed_players_if_empty
from fantasy_draft.providers import DynastyProcessProvider, FantasyProsProvider
from fantasy_draft.services import DraftNotFoundError, DraftService, PlayerNotFoundError
from fantasy_draft.settings import AppSettings, PROJECT_ROOT


class PickRequest(BaseModel):
    player_id: str


class CorrectionRequest(BaseModel):
    player_id: str


def _redirect(message: str = "", error: str = "") -> RedirectResponse:
    query = urlencode({key: value for key, value in {"message": message, "error": error}.items() if value})
    return RedirectResponse(url=f"/{'?' + query if query else ''}", status_code=303)


def _state_payload(state, recommendations, evaluated=()) -> dict:
    analytics = {item.player.id: item for item in evaluated}
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
                "age": age_on(player.birth_date),
                "experience": experience_label(player.draft_year, state.season),
                "draft_year": player.draft_year,
                "overall_rank": player.overall_rank,
                "adp": player.adp,
                "tier": player.tier,
                "projected_points": player.projected_points,
                "analytics": (
                    {
                        "league_projected_points": analytics[player.id].projected_points,
                        "vor": analytics[player.id].vor,
                        "replacement_level": analytics[player.id].replacement_level,
                        "provider_tier": analytics[player.id].provider_tier,
                        "our_tier": analytics[player.id].our_tier,
                        "tier_cliff": analytics[player.id].tier_cliff,
                        "scarcity": analytics[player.id].scarcity,
                        "survival_probability": analytics[player.id].survival_probability,
                        "cost_of_waiting": analytics[player.id].cost_of_waiting,
                        "quantitative_score": analytics[player.id].quantitative_score,
                    }
                    if player.id in analytics else None
                ),
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
        "recommendation_reason": recommendations.reason,
        "recommendation_confidence": recommendations.overall_confidence,
        "recommendation_latency_ms": recommendations.latency_ms,
        "recommendation_is_fallback": recommendations.is_fallback,
        "recommendation_fallback_reason": recommendations.fallback_reason,
        "recommendation_configured_model": recommendations.configured_model,
        "recommendation_reasoning_effort": recommendations.reasoning_effort,
        "recommendation_timeout_seconds": recommendations.configured_timeout_seconds,
        "recommendation_response_status": recommendations.response_status,
        "next_pick_strategy": recommendations.next_pick_strategy,
    }


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or AppSettings.from_environment()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    service = DraftService(session_factory)
    import_service = DataImportService(
        session_factory, settings.cache_dir, data_mode=settings.fantasypros_data_mode
    )
    evaluator = BaselinePlayerEvaluator(
        session_factory, simulations=settings.survival_simulations
    )
    offline_advisor = OfflineStrategicAdvisor()
    openai_advisor = OpenAIStrategicAdvisor(
        session_factory,
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        timeout_seconds=settings.openai_live_timeout_seconds,
        reasoning_effort=settings.openai_reasoning_effort,
    )
    advisor = ResilientStrategicAdvisor(
        openai_advisor,
        offline_advisor,
    )
    openai_diagnostic_advisor = OpenAIStrategicAdvisor(
        session_factory,
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        timeout_seconds=settings.openai_diagnostic_timeout_seconds,
        reasoning_effort=settings.openai_reasoning_effort,
    )
    advisor_lock = Lock()
    fantasypros_provider = FantasyProsProvider(
        settings.fantasypros_api_key,
        base_url=settings.fantasypros_base_url,
        timeout_seconds=settings.fantasypros_timeout_seconds,
    )
    crosswalk_provider = DynastyProcessProvider(
        settings.dynastyprocess_url,
        timeout_seconds=settings.fantasypros_timeout_seconds,
    )
    exporter = DraftExporter(session_factory)
    backup_service = DatabaseBackupService(engine, settings.backup_dir)
    readiness_service = ReadinessService(
        session_factory,
        service,
        import_service,
        evaluator,
        exporter,
        backup_dir=settings.backup_dir,
        backup_service=backup_service,
        fantasypros_provider=fantasypros_provider if settings.fantasypros_api_key else None,
        ai_diagnostic_advisor=(
            openai_diagnostic_advisor if settings.openai_api_key else None
        ),
        openai_configured=bool(settings.openai_api_key),
    )
    templates = Jinja2Templates(directory=PROJECT_ROOT / "templates")
    asset_digest = hashlib.sha256()
    for asset in (PROJECT_ROOT / "static" / "style.css", PROJECT_ROOT / "static" / "app.js"):
        asset_digest.update(asset.read_bytes())
    templates.env.globals["asset_version"] = asset_digest.hexdigest()[:12]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        run_migrations(engine)
        config = load_league_config(settings.league_config_path)
        with session_factory.begin() as session:
            seed_players_if_empty(session, settings.player_data_path)
        import_service.backfill_cached_player_demographics()
        service.get_or_create_active_draft(config)
        yield
        engine.dispose()

    app = FastAPI(title="Fantasy Draft AI", version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "static"), name="static")
    app.state.draft_service = service
    app.state.session_factory = session_factory
    app.state.import_service = import_service
    app.state.readiness_service = readiness_service
    app.state.openai_advisor = openai_advisor

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
        evaluation_by_id = {item.player.id: item for item in evaluated}
        available = list(visible_state.available_players)
        if sort in {"rank", "adp", "tier", "projection"}:
            keys = {
                "rank": lambda player: evaluation_by_id[player.id].ecr or 9999,
                "adp": lambda player: evaluation_by_id[player.id].adp or 9999,
                "tier": lambda player: evaluation_by_id[player.id].our_tier,
                "projection": lambda player: -evaluation_by_id[player.id].projected_points,
            }
            available.sort(key=lambda player: (keys[sort](player), player.name))
        with advisor_lock:
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
        position_urls = {}
        for selected_position in ["", *positions]:
            query = {"sort": sort}
            if search:
                query["search"] = search
            if selected_position:
                query["position"] = selected_position
            position_urls[selected_position] = f"/?{urlencode(query)}"
        player_demographics = {
            player.id: {
                "age": age_on(player.birth_date),
                "experience": experience_label(player.draft_year, state.season),
            }
            for player in available
        }
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "state": state,
                "available": available,
                "recommendations": recommendations,
                "teams": teams,
                "team_names": team_names,
                "current_team": current_team,
                "teams_before": list(dict.fromkeys(state.teams_before_user_pick)),
                "positions": positions,
                "position_urls": position_urls,
                "player_demographics": player_demographics,
                "pick_lookup": pick_lookup,
                "rosters": rosters,
                "search": search,
                "position": position,
                "sort": sort,
                "message": message,
                "error": error,
                "drafts": service.list_drafts(),
                "evaluation_by_id": evaluation_by_id,
                "openai_models": settings.openai_live_models,
                "active_openai_model": openai_advisor.model,
            },
        )

    @app.get("/drafts", response_class=HTMLResponse)
    def draft_sessions(request: Request, message: str = "", error: str = ""):
        master = load_league_config(settings.league_config_path)
        return templates.TemplateResponse(
            request=request,
            name="drafts.html",
            context={
                "drafts": service.list_drafts(),
                "current_draft_id": service.current_draft_id(),
                "master": master,
                "message": message,
                "error": error,
                "current_year": date.today().year,
            },
        )

    @app.get("/data", response_class=HTMLResponse)
    def data_status(request: Request, message: str = "", error: str = ""):
        state = service.get_state(active_draft_id())
        return templates.TemplateResponse(
            request=request,
            name="data.html",
            context={
                "status": import_service.status(),
                "state": state,
                "key_configured": bool(settings.fantasypros_api_key),
                "message": message,
                "error": error,
            },
        )

    @app.post("/data/refresh")
    def refresh_data():
        state = service.get_state(active_draft_id())
        positions = sorted({
            position
            for slot in state.config.roster.slots if slot.draftable
            for position in slot.eligible_positions
        })
        receptions = float(state.config.scoring.get("receiving", {}).get("receptions", 0))
        scoring = "PPR" if receptions >= .75 else "HALF" if receptions >= .25 else "STD"
        result = import_service.refresh_all(
            fantasypros_provider,
            crosswalk_provider,
            season=state.season or date.today().year,
            scoring=scoring,
            positions=positions,
        )
        if result.runs:
            try:
                service.attach_data_snapshot(state.draft_id, result.runs)
            except DraftRuleError as exc:
                result.warnings.append(str(exc))
        if result.warnings:
            query = urlencode({"error": "; ".join(result.warnings)})
        else:
            query = urlencode({"message": f"Completed {len(result.runs)} data imports."})
        return RedirectResponse(f"/data?{query}", status_code=303)

    @app.post("/drafts")
    def create_draft_session(
        name: str = Form(),
        season: int = Form(),
        draft_kind: str = Form(),
        team_count: int = Form(),
        our_draft_slot: int = Form(),
        start_immediately: bool = Form(default=False),
    ):
        try:
            master = load_league_config(settings.league_config_path)
            config = configure_draft_session(
                master, team_count=team_count, our_draft_slot=our_draft_slot
            )
            draft_id = service.create_draft(
                config,
                name=name.strip(),
                season=season,
                draft_kind=draft_kind,
                status="active" if start_immediately else "setup",
            )
            return _redirect(message=f"Created draft {draft_id}: {name}.")
        except (ValueError, DraftRuleError) as exc:
            query = urlencode({"error": str(exc)})
            return RedirectResponse(f"/drafts?{query}", status_code=303)

    @app.post("/drafts/{draft_id}/switch")
    def switch_draft(draft_id: int):
        try:
            service.switch_draft(draft_id)
            return _redirect(message=f"Switched to draft {draft_id}.")
        except DraftNotFoundError as exc:
            return _redirect(error=str(exc))

    @app.post("/drafts/{draft_id}/activate")
    def activate_draft(draft_id: int):
        try:
            service.activate_draft(draft_id)
            return _redirect(message=f"Draft {draft_id} is active.")
        except (DraftRuleError, DraftNotFoundError) as exc:
            return _redirect(error=str(exc))

    @app.post("/drafts/{draft_id}/archive")
    def archive_draft(draft_id: int):
        try:
            service.archive_draft(draft_id)
            query = urlencode({"message": f"Archived draft {draft_id}; all picks were preserved."})
            return RedirectResponse(f"/drafts?{query}", status_code=303)
        except DraftNotFoundError as exc:
            return _redirect(error=str(exc))

    @app.post("/drafts/{draft_id}/reset")
    def reset_draft(draft_id: int):
        try:
            new_id = service.reset_draft(draft_id)
            return _redirect(message=f"Archived draft {draft_id} and started empty draft {new_id}.")
        except (DraftRuleError, DraftNotFoundError) as exc:
            return _redirect(error=str(exc))

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

    @app.post("/advisor/retry")
    def retry_advisor(model: str = Form()):
        if model not in settings.openai_live_models:
            return _redirect(error="That AI model is not enabled for live drafting.")
        state = service.get_state(active_draft_id())
        if state.current_team_id != state.config.draft.our_team_id:
            return _redirect(
                error="AI requests are available only while our team is on the clock."
            )
        evaluated = evaluator.evaluate(state, state.available_players)
        try:
            with advisor_lock:
                openai_advisor.model = model
                result = openai_advisor.recommend(
                    state,
                    evaluated,
                    force=True,
                    persist=True,
                    use_cache=False,
                )
            return _redirect(
                message=(
                    f"AI recommendation updated with {result.model_used or model} "
                    f"in {(result.latency_ms or 0) / 1000:.2f}s."
                )
            )
        except Exception as exc:
            category = classify_openai_failure(exc)
            return _redirect(
                error=(
                    f"{model} failed ({category}); deterministic recommendations "
                    "remain active. Choose another model or try again."
                )
            )

    @app.get("/drafts/{draft_id}/export.json")
    def export_json(draft_id: int):
        try:
            content = exporter.json_text(draft_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(content, media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="draft-{draft_id}.json"'
        })

    @app.get("/drafts/{draft_id}/export.csv")
    def export_csv(draft_id: int):
        try:
            content = exporter.csv_text(draft_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(content, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="draft-{draft_id}-picks.csv"'
        })

    @app.post("/backup")
    def create_backup():
        try:
            path = backup_service.create(label=f"draft-{active_draft_id()}")
            return _redirect(message=f"Database backup created: {path.name}")
        except Exception as exc:
            return _redirect(error=f"Backup failed ({type(exc).__name__}); pick entry remains available.")

    @app.get("/readiness", response_class=HTMLResponse)
    def readiness_page(request: Request):
        return templates.TemplateResponse(request=request, name="readiness.html", context={
            "report": None, "draft_id": active_draft_id()
        })

    @app.post("/readiness", response_class=HTMLResponse)
    def run_readiness(request: Request):
        draft_id = active_draft_id()
        report = readiness_service.run(draft_id, live_external_checks=True)
        return templates.TemplateResponse(request=request, name="readiness.html", context={
            "report": report, "draft_id": draft_id
        })

    @app.get("/api/state")
    def api_state():
        state = service.get_state(active_draft_id())
        evaluated = evaluator.evaluate(state, state.available_players)
        with advisor_lock:
            recommendations = advisor.recommend(state, evaluated)
        return _state_payload(state, recommendations, evaluated)

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
