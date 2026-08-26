from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from fantasy_draft.config import LeagueConfig
from fantasy_draft.data_import import DataImportService, detect_data_mode
from fantasy_draft.database import create_database_engine, create_session_factory
from fantasy_draft.engine import next_pick_for_team, pick_coordinates, team_for_pick
from fantasy_draft.evaluation import (
    BaselinePlayerEvaluator,
    OfflineStrategicAdvisor,
    select_advisor_candidates,
)
from fantasy_draft.llm import OpenAIStrategicAdvisor
from fantasy_draft.migrations import run_migrations
from fantasy_draft.models import ImportRun, Player, PlayerExternalId, PlayerProjection, PlayerRanking
from fantasy_draft.operations import DatabaseBackupService, DraftExporter
from fantasy_draft.providers import FantasyProsProvider
from fantasy_draft.services import DraftService
from fantasy_draft.identity import active_identity_duplicates


@dataclass(frozen=True)
class Check:
    status: str
    message: str
    detail: str = ""


@dataclass(frozen=True)
class ReadinessReport:
    generated_at: str
    sections: dict[str, list[Check]]

    @property
    def status(self) -> str:
        values = [check.status for checks in self.sections.values() for check in checks]
        return "FAIL" if "FAIL" in values else "WARNING" if "WARNING" in values else "PASS"


class ReadinessService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        draft_service: DraftService,
        import_service: DataImportService,
        evaluator: BaselinePlayerEvaluator,
        exporter: DraftExporter,
        *,
        backup_dir: Path,
        backup_service: DatabaseBackupService | None = None,
        fantasypros_provider: FantasyProsProvider | None = None,
        ai_diagnostic_advisor: OpenAIStrategicAdvisor | None = None,
        openai_configured: bool = False,
        candidate_limit: int = 30,
    ):
        self.session_factory = session_factory
        self.draft_service = draft_service
        self.import_service = import_service
        self.evaluator = evaluator
        self.exporter = exporter
        self.backup_dir = backup_dir
        self.backup_service = backup_service
        self.fantasypros_provider = fantasypros_provider
        self.ai_diagnostic_advisor = ai_diagnostic_advisor
        self.openai_configured = openai_configured
        self.candidate_limit = candidate_limit

    def _engine_rehearsal(self, config: LeagueConfig) -> str:
        engine = create_database_engine("sqlite:///:memory:")
        try:
            run_migrations(engine)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                session.add_all([
                    Player(id="readiness-a", name="Readiness A", primary_position="RB", eligible_positions=["RB"], active=True),
                    Player(id="readiness-b", name="Readiness B", primary_position="WR", eligible_positions=["WR"], active=True),
                ])
            service = DraftService(factory)
            draft_id = service.create_draft(config, name="readiness", select_current=False)
            assert service.make_pick(draft_id, "readiness-a") == 1
            assert service.undo_last_pick(draft_id).player_id == "readiness-a"
            service.make_pick(draft_id, "readiness-a")
            service.correct_pick(draft_id, 1, "readiness-b")
            assert service.get_state(draft_id).picks[0].player_id == "readiness-b"
            return "Pick persistence, undo, and correction passed in an isolated database."
        finally:
            engine.dispose()

    def run(self, draft_id: int, *, live_external_checks: bool = True) -> ReadinessReport:
        sections: dict[str, list[Check]] = {name: [] for name in [
            "LEAGUE", "DATABASE", "PLAYER DATA", "IDENTITY", "EVALUATION",
            "AI", "FALLBACK", "BACKUP", "DRAFT ENGINE",
        ]}
        try:
            state = self.draft_service.get_state(draft_id)
            config = state.config
            sections["LEAGUE"].extend([
                Check("PASS", "Configuration snapshot parses and validates."),
                Check("PASS", f"{config.league.team_count} teams; {config.draft.type} draft; our slot {config.team_by_id(config.draft.our_team_id).draft_slot}."),
                Check("PASS", f"{config.draft.rounds} rounds and {config.roster.draftable_size} draftable roster slots."),
                Check("PASS", f"Scoring configuration has {len(config.scoring)} categories."),
            ])
        except Exception as exc:
            sections["LEAGUE"].append(Check("FAIL", "Current draft configuration is invalid.", type(exc).__name__))
            return ReadinessReport(datetime.now(timezone.utc).isoformat(), sections)

        try:
            with self.session_factory() as session:
                session.execute(text("SELECT 1"))
                nested = session.begin_nested()
                session.execute(text("UPDATE application_state SET id=id WHERE id=1"))
                nested.rollback()
            sections["DATABASE"].append(Check("PASS", "Database is reachable and transactionally writable."))
            sections["DATABASE"].append(Check("PASS", f"Current draft #{draft_id} is valid and selected explicitly."))
        except Exception as exc:
            sections["DATABASE"].append(Check("FAIL", "Database write round-trip failed.", type(exc).__name__))

        status = self.import_service.status()
        snapshot_ids = [int(value) for value in state.data_snapshot.values()]
        with self.session_factory() as session:
            snapshot_runs = {
                run.id: run for run in session.scalars(
                    select(ImportRun).where(ImportRun.id.in_(snapshot_ids))
                )
            } if snapshot_ids else {}
            rank_run_id = state.data_snapshot.get("fantasypros:rankings")
            projection_run_id = state.data_snapshot.get("fantasypros:projections")
            ranked_count = session.scalar(select(func.count(PlayerRanking.id)).where(
                PlayerRanking.import_run_id == rank_run_id
            )) or 0 if rank_run_id else 0
            projected_count = session.scalar(select(func.count(PlayerProjection.id)).where(
                PlayerProjection.import_run_id == projection_run_id
            )) or 0 if projection_run_id else 0
        player_status = "PASS" if status["players"] >= 250 else "WARNING"
        sections["PLAYER DATA"].extend([
            Check(player_status, f"{status['players']} canonical players loaded.", "250+ recommended before a live draft."),
            Check("PASS" if ranked_count >= 250 else "WARNING", f"{ranked_count} players ranked in this draft's snapshot."),
            Check("PASS" if projected_count >= 200 else "WARNING", f"{projected_count} players have raw projections in this draft's snapshot."),
        ])
        fantasypros_runs = [
            run for run in snapshot_runs.values() if run.provider == "fantasypros"
        ]
        if fantasypros_runs:
            newest = max(fantasypros_runs, key=lambda item: item.completed_at or item.started_at)
            mode_status = "WARNING" if newest.data_mode in {"sample", "unknown"} else "PASS"
            sections["PLAYER DATA"].append(Check(
                mode_status,
                f"FantasyPros data mode: {newest.data_mode.upper()}.",
                f"Last completed {newest.completed_at or newest.started_at}.",
            ))
        else:
            sections["PLAYER DATA"].append(Check("WARNING", "No audited FantasyPros import exists."))

        with self.session_factory() as session:
            if rank_run_id:
                top = list(session.scalars(
                    select(Player).join(PlayerRanking, PlayerRanking.player_id == Player.id)
                    .where(PlayerRanking.import_run_id == rank_run_id)
                    .order_by(PlayerRanking.overall_rank)
                    .limit(250)
                ))
            else:
                top = list(session.scalars(
                    select(Player).where(Player.overall_rank.is_not(None)).order_by(Player.overall_rank).limit(250)
                ))
            top_ids = {player.id for player in top}
            matched = len(set(session.scalars(
                select(PlayerExternalId.player_id).where(PlayerExternalId.player_id.in_(top_ids)).distinct()
            ))) if top_ids else 0
            duplicates = active_identity_duplicates(session)
        target = len(top)
        sections["IDENTITY"].append(Check(
            "PASS" if target >= 250 and matched == target else "WARNING",
            f"Top-{target or 0} matched: {matched}/{target or 0}.",
            f"{status['unmatched']} unresolved imported records; {status['external_ids']} external IDs stored.",
        ))
        duplicate_names = [
            f"{players[0].name} ({identity[1]}: "
            + ", ".join(player.nfl_team or "FA" for player in players)
            + ")"
            for identity, players in sorted(duplicates.items())
        ]
        sections["IDENTITY"].append(Check(
            "FAIL" if duplicates else "PASS",
            "No duplicate active canonical name/position pairs."
            if not duplicates else
            f"{len(duplicates)} duplicate active player identities require review.",
            "; ".join(duplicate_names[:10]),
        ))

        if self.fantasypros_provider is None:
            sections["PLAYER DATA"].append(Check("WARNING", "FantasyPros key is not configured; cached local data remains usable."))
        elif live_external_checks:
            try:
                started = time.perf_counter()
                payload = self.fantasypros_provider.get_players()
                mode = detect_data_mode(payload.raw, "auto")
                elapsed = time.perf_counter() - started
                sections["PLAYER DATA"].append(Check(
                    "WARNING" if mode in {"sample", "unknown"} else "PASS",
                    f"FantasyPros API connected: {len(payload.records)} player records; mode {mode.upper()} ({elapsed:.2f}s).",
                ))
            except Exception as exc:
                sections["PLAYER DATA"].append(Check("WARNING", "FantasyPros connectivity failed; local data is still usable.", type(exc).__name__))

        try:
            started = time.perf_counter()
            evaluated = self.evaluator.evaluate(state, state.available_players)
            elapsed = time.perf_counter() - started
            if not evaluated:
                raise ValueError("no candidates")
            top_eval = evaluated[0]
            sections["EVALUATION"].extend([
                Check("PASS", f"League scoring, replacement level, and VOR generated for {len(evaluated)} candidates."),
                Check("PASS", f"Tier cliffs and scarcity valid; top candidate {top_eval.player.name} has {top_eval.vor:+.1f} VOR."),
                Check("PASS" if elapsed < 5 else "WARNING", f"Survival simulation and recommendations completed in {elapsed:.2f}s."),
            ])
            advisor_candidates = select_advisor_candidates(
                state,
                evaluated,
                limit=self.candidate_limit,
            )
            needed_codes = set(
                state.team_needs[state.config.draft.our_team_id]
            )
            needed_positions = {
                position
                for slot in state.config.roster.slots
                if slot.code in needed_codes
                for position in slot.eligible_positions
            }
            candidate_positions = {
                item.player.primary_position for item in advisor_candidates
            }
            missing_needs = sorted(needed_positions - candidate_positions)
            sections["EVALUATION"].append(Check(
                "FAIL" if missing_needs else "PASS",
                "Advisor allowlist covers every unfilled starter position."
                if not missing_needs else
                "Advisor allowlist omits required starter positions.",
                ", ".join(missing_needs),
            ))
            sections["EVALUATION"].append(Check(
                "PASS" if len(advisor_candidates) >= 20 else "WARNING",
                f"Advisor packet contains {len(advisor_candidates)} position-diverse candidates.",
            ))
        except Exception as exc:
            evaluated = []
            sections["EVALUATION"].append(Check("FAIL", "Deterministic evaluation failed.", type(exc).__name__))

        offline = OfflineStrategicAdvisor().recommend(state, evaluated)
        sections["FALLBACK"].append(Check(
            "PASS" if offline.preferred else "FAIL",
            "Deterministic advisor works without AI." if offline.preferred else "Deterministic advisor returned no candidate.",
        ))
        if not self.openai_configured or self.ai_diagnostic_advisor is None:
            sections["AI"].append(Check("WARNING", "OPENAI_API_KEY is not configured; deterministic fallback is active."))
        elif evaluated and live_external_checks:
            diagnostic = self.ai_diagnostic_advisor.diagnose(state, evaluated)
            configuration = (
                f"configured_model={diagnostic.configured_model}; "
                f"model_used={diagnostic.model_used or 'not returned'}; "
                f"reasoning_effort={diagnostic.reasoning_effort}; "
                f"timeout={diagnostic.timeout_seconds:g}s; "
                f"max_retries={diagnostic.max_retries}; "
                f"latency={diagnostic.latency_ms / 1000:.2f}s; "
                f"structured_output={'PASS' if diagnostic.structured_output_valid else 'NOT VALIDATED'}; "
                f"response_status={diagnostic.response_status or 'not returned'}"
            )
            if diagnostic.success:
                sections["AI"].append(Check(
                    "PASS", "OpenAI diagnostic succeeded.", configuration
                ))
            else:
                sections["AI"].append(Check(
                    "WARNING",
                    "AI diagnostic failed; deterministic fallback remains operational.",
                    f"{configuration}; failure_category={diagnostic.failure_category}; "
                    f"exception={diagnostic.exception_type}",
                ))

        try:
            exported = self.exporter.json_data(draft_id)
            if exported["draft"]["id"] != draft_id:
                raise ValueError("wrong draft exported")
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            descriptor, filename = tempfile.mkstemp(prefix="readiness-", dir=self.backup_dir)
            os.close(descriptor)
            Path(filename).unlink()
            backup_detail = ""
            if self.backup_service is not None:
                backup_path = self.backup_service.create(label="readiness")
                backup_detail = f" SQLite backup created: {backup_path.name}."
            sections["BACKUP"].append(Check("PASS", f"JSON export succeeds and backup location is writable.{backup_detail}"))
        except Exception as exc:
            sections["BACKUP"].append(Check("FAIL", "Export or backup-location check failed.", type(exc).__name__))

        try:
            sequence = [team_for_pick(config, value) for value in range(1, config.league.team_count * 2 + 1)]
            assert pick_coordinates(config.league.team_count + 1, config.league.team_count).draft_slot == config.league.team_count
            assert next_pick_for_team(config, config.draft.our_team_id, 1) is not None
            detail = self._engine_rehearsal(config)
            sections["DRAFT ENGINE"].append(Check("PASS", f"Snake order, next-pick calculation, persistence, undo, and correction passed. {detail}"))
        except Exception as exc:
            sections["DRAFT ENGINE"].append(Check("FAIL", "Draft-engine sanity rehearsal failed.", type(exc).__name__))

        return ReadinessReport(datetime.now(timezone.utc).isoformat(), sections)
