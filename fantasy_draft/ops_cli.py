from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from fantasy_draft.config import configure_draft_session, load_league_config
from fantasy_draft.data_import import DataImportService
from fantasy_draft.database import create_database_engine, create_session_factory
from fantasy_draft.evaluation import BaselinePlayerEvaluator
from fantasy_draft.llm import OpenAIStrategicAdvisor
from fantasy_draft.migrations import run_migrations
from fantasy_draft.operations import DatabaseBackupService, DraftExporter
from fantasy_draft.players import seed_players_if_empty
from fantasy_draft.providers import DynastyProcessProvider, FantasyProsProvider
from fantasy_draft.readiness import ReadinessService
from fantasy_draft.services import DraftService
from fantasy_draft.engine import DraftRuleError
from fantasy_draft.settings import AppSettings


def _services(settings: AppSettings):
    engine = create_database_engine(settings.database_url)
    run_migrations(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        seed_players_if_empty(session, settings.player_data_path)
    service = DraftService(factory)
    draft_id = service.get_or_create_active_draft(load_league_config(settings.league_config_path))
    importer = DataImportService(factory, settings.cache_dir, data_mode=settings.fantasypros_data_mode)
    importer.backfill_cached_player_demographics()
    evaluator = BaselinePlayerEvaluator(factory, simulations=settings.survival_simulations)
    exporter = DraftExporter(factory)
    backup_service = DatabaseBackupService(engine, settings.backup_dir)
    fp = FantasyProsProvider(
        settings.fantasypros_api_key,
        base_url=settings.fantasypros_base_url,
        timeout_seconds=settings.fantasypros_timeout_seconds,
    )
    live_ai = OpenAIStrategicAdvisor(
        factory, api_key=settings.openai_api_key, model=settings.openai_model,
        timeout_seconds=settings.openai_live_timeout_seconds,
        reasoning_effort=settings.openai_reasoning_effort,
    )
    diagnostic_ai = OpenAIStrategicAdvisor(
        factory, api_key=settings.openai_api_key, model=settings.openai_model,
        timeout_seconds=settings.openai_diagnostic_timeout_seconds,
        reasoning_effort=settings.openai_reasoning_effort,
    )
    return (
        engine, factory, service, draft_id, importer, evaluator, exporter,
        backup_service, fp, live_ai, diagnostic_ai,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fantasy Draft AI operational commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("refresh-data", help="Refresh FantasyPros and DynastyProcess data")
    new_parser = subparsers.add_parser("new-draft", help="Create and select a new draft session")
    new_parser.add_argument("--name", required=True)
    new_parser.add_argument("--season", type=int, default=date.today().year)
    new_parser.add_argument("--kind", choices=["practice", "live"], default="practice")
    new_parser.add_argument("--team-count", type=int)
    new_parser.add_argument("--our-slot", type=int)
    new_parser.add_argument("--setup", action="store_true", help="Create without activating")
    readiness_parser = subparsers.add_parser("readiness", help="Run the pre-draft readiness check")
    readiness_parser.add_argument("--offline", action="store_true", help="Skip live provider diagnostics")
    export_parser = subparsers.add_parser("export", help="Export a draft")
    export_parser.add_argument("--draft-id", type=int)
    export_parser.add_argument("--format", choices=["json", "csv"], default="json")
    export_parser.add_argument("--output", type=Path, required=True)
    subparsers.add_parser("backup", help="Create a consistent SQLite database backup")
    args = parser.parse_args()
    settings = AppSettings.from_environment()
    (
        engine, factory, service, draft_id, importer, evaluator, exporter,
        backup_service, fp, _live_ai, diagnostic_ai,
    ) = _services(settings)
    try:
        if args.command == "new-draft":
            master = load_league_config(settings.league_config_path)
            config = configure_draft_session(
                master,
                team_count=args.team_count or master.league.team_count,
                our_draft_slot=args.our_slot or master.team_by_id(master.draft.our_team_id).draft_slot,
            )
            created = service.create_draft(
                config, name=args.name, season=args.season, draft_kind=args.kind,
                status="setup" if args.setup else "active",
            )
            print(f"Created and selected {args.kind} draft {created}: {args.name}")
        elif args.command == "refresh-data":
            state = service.get_state(draft_id)
            positions = sorted({
                position for slot in state.config.roster.slots if slot.draftable
                for position in slot.eligible_positions
            })
            receptions = float(state.config.scoring.get("receiving", {}).get("receptions", 0))
            scoring = "PPR" if receptions >= .75 else "HALF" if receptions >= .25 else "STD"
            result = importer.refresh_all(
                fp,
                DynastyProcessProvider(settings.dynastyprocess_url, settings.fantasypros_timeout_seconds),
                season=state.season or date.today().year,
                scoring=scoring,
                positions=positions,
            )
            if result.runs:
                try:
                    service.attach_data_snapshot(draft_id, result.runs)
                except DraftRuleError as exc:
                    result.warnings.append(str(exc))
            print(f"Completed import runs: {len(result.runs)}")
            for warning in result.warnings:
                print(f"WARNING: {warning}")
        elif args.command == "readiness":
            report = ReadinessService(
                factory, service, importer, evaluator, exporter,
                backup_dir=settings.backup_dir,
                backup_service=backup_service,
                fantasypros_provider=fp if settings.fantasypros_api_key else None,
                ai_diagnostic_advisor=(
                    diagnostic_ai if settings.openai_api_key else None
                ),
                openai_configured=bool(settings.openai_api_key),
            ).run(draft_id, live_external_checks=not args.offline)
            print(f"{report.status} — DRAFT #{draft_id}")
            for section, checks in report.sections.items():
                print(section)
                for check in checks:
                    print(f"  {check.status}: {check.message} {check.detail}".rstrip())
        elif args.command == "export":
            selected_id = args.draft_id or draft_id
            content = exporter.json_text(selected_id) if args.format == "json" else exporter.csv_text(selected_id)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
            print(f"Exported draft {selected_id} to {args.output}")
        elif args.command == "backup":
            path = backup_service.create(label=f"draft-{draft_id}")
            print(f"Backup created: {path}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
