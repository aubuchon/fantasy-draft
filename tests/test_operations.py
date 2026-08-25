from __future__ import annotations

import csv
import io
import sqlite3

from sqlalchemy import func, select

from fantasy_draft.data_import import DataImportService
from fantasy_draft.evaluation import BaselinePlayerEvaluator
from fantasy_draft.models import Draft, DraftPick, ImportRun, PlayerExternalId, utc_now
from fantasy_draft.llm import AdvisorDiagnostic
from fantasy_draft.operations import DatabaseBackupService, DraftExporter
from fantasy_draft.readiness import ReadinessService


def test_json_and_csv_export_include_reconstruction_data_without_secrets(service):
    draft_service, draft_id, session_factory = service
    with session_factory.begin() as session:
        session.add(PlayerExternalId(
            player_id="player-1", provider="fantasypros", external_id="fp-1", source="test"
        ))
    draft_service.make_pick(draft_id, "player-1")
    exporter = DraftExporter(session_factory)
    data = exporter.json_data(draft_id)
    assert data["draft"]["id"] == draft_id
    assert data["configuration_snapshot"]["league"]["team_count"] == 8
    assert data["selections"][0]["player"]["external_ids"]["fantasypros"] == "fp-1"
    assert data["rosters"]["team-1"][0]["overall_pick"] == 1
    assert "api_key" not in exporter.json_text(draft_id).lower()

    rows = list(csv.DictReader(io.StringIO(exporter.csv_text(draft_id))))
    assert rows[0]["overall_pick"] == "1"
    assert rows[0]["fantasypros_id"] == "fp-1"


def test_sqlite_backup_is_consistent_and_readable(service, tmp_path):
    draft_service, draft_id, session_factory = service
    draft_service.make_pick(draft_id, "player-1")
    engine = session_factory.kw["bind"]
    path = DatabaseBackupService(engine, tmp_path / "backups").create(label="test")
    with sqlite3.connect(path) as connection:
        assert connection.execute("select count(*) from draft_picks").fetchone()[0] == 1


def test_data_snapshot_can_only_change_before_first_pick(service):
    draft_service, draft_id, session_factory = service
    with session_factory.begin() as session:
        run = ImportRun(
            provider="fantasypros", dataset="rankings", season=2026,
            status="success", data_mode="sample", completed_at=utc_now(),
        )
        session.add(run)
        session.flush()
        run_id = run.id
    draft_service.attach_data_snapshot(draft_id, [run_id])
    with session_factory() as session:
        assert session.get(Draft, draft_id).data_snapshot["fantasypros:rankings"] == run_id
    draft_service.make_pick(draft_id, "player-1")
    try:
        draft_service.attach_data_snapshot(draft_id, [run_id])
        assert False, "expected snapshot mutation to be rejected"
    except ValueError as exc:
        assert "before the first pick" in str(exc)


def test_readiness_check_uses_isolated_draft_engine(service, tmp_path):
    draft_service, draft_id, session_factory = service
    exporter = DraftExporter(session_factory)
    readiness = ReadinessService(
        session_factory,
        draft_service,
        DataImportService(session_factory, tmp_path / "cache"),
        BaselinePlayerEvaluator(session_factory, simulations=100),
        exporter,
        backup_dir=tmp_path / "backups",
        backup_service=DatabaseBackupService(session_factory.kw["bind"], tmp_path / "backups"),
    )
    report = readiness.run(draft_id, live_external_checks=False)
    assert report.status == "WARNING"
    assert any(check.status == "PASS" for check in report.sections["DRAFT ENGINE"])
    assert any("fallback" in check.message.lower() or "without AI" in check.message for check in report.sections["FALLBACK"])
    assert draft_service.get_state(draft_id).picks == []


def test_readiness_reports_diagnostic_configuration_and_failure_category(service, tmp_path):
    draft_service, draft_id, session_factory = service

    class ReadTimeoutDiagnostic:
        def diagnose(self, state, evaluated):
            return AdvisorDiagnostic(
                success=False,
                configured_model="gpt-5.6",
                model_used=None,
                reasoning_effort="low",
                timeout_seconds=30,
                max_retries=0,
                latency_ms=30011,
                structured_output_valid=False,
                failure_category="timeout.read",
                exception_type="APITimeoutError",
            )

    report = ReadinessService(
        session_factory,
        draft_service,
        DataImportService(session_factory, tmp_path / "cache"),
        BaselinePlayerEvaluator(session_factory, simulations=50),
        DraftExporter(session_factory),
        backup_dir=tmp_path / "backups",
        ai_diagnostic_advisor=ReadTimeoutDiagnostic(),
        openai_configured=True,
    ).run(draft_id, live_external_checks=True)
    check = report.sections["AI"][0]
    assert check.status == "WARNING"
    assert "reasoning_effort=low" in check.detail
    assert "timeout=30s" in check.detail
    assert "latency=30.01s" in check.detail
    assert "failure_category=timeout.read" in check.detail
    assert "exception=APITimeoutError" in check.detail
