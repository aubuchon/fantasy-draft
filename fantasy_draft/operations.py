from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from fantasy_draft.models import (
    Draft,
    DraftPick,
    ImportRun,
    PlayerExternalId,
    RecommendationHistory,
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


class DraftExporter:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def _load(self, session: Session, draft_id: int) -> Draft:
        draft = session.scalar(
            select(Draft)
            .where(Draft.id == draft_id)
            .options(
                selectinload(Draft.teams),
                selectinload(Draft.picks).selectinload(DraftPick.player),
            )
        )
        if draft is None:
            raise LookupError(f"draft {draft_id} does not exist")
        return draft

    def json_data(self, draft_id: int) -> dict[str, Any]:
        with self.session_factory() as session:
            draft = self._load(session, draft_id)
            picks = sorted(draft.picks, key=lambda item: item.overall_pick)
            player_ids = {pick.player_id for pick in picks}
            external: dict[str, dict[str, str]] = {player_id: {} for player_id in player_ids}
            if player_ids:
                for row in session.scalars(
                    select(PlayerExternalId).where(PlayerExternalId.player_id.in_(player_ids))
                ):
                    external[row.player_id][row.provider] = row.external_id
            run_ids = [int(value) for value in (draft.data_snapshot or {}).values()]
            runs = {
                run.id: {
                    "provider": run.provider,
                    "dataset": run.dataset,
                    "season": run.season,
                    "week": run.week,
                    "data_mode": run.data_mode,
                    "completed_at": _iso(run.completed_at),
                    "source_checksum": run.source_checksum,
                }
                for run in session.scalars(select(ImportRun).where(ImportRun.id.in_(run_ids)))
            } if run_ids else {}
            selections = [
                {
                    "overall_pick": pick.overall_pick,
                    "round": pick.round_number,
                    "pick_in_round": pick.pick_in_round,
                    "team_id": pick.team_id,
                    "player": {
                        "id": pick.player.id,
                        "name": pick.player.name,
                        "position": pick.player.primary_position,
                        "nfl_team": pick.player.nfl_team,
                        "external_ids": external.get(pick.player_id, {}),
                    },
                    "market_adp": pick.market_adp,
                    "market_rank": pick.market_rank,
                    "selected_at": _iso(pick.selected_at),
                }
                for pick in picks
            ]
            rosters = {
                team.team_id: [item for item in selections if item["team_id"] == team.team_id]
                for team in sorted(draft.teams, key=lambda item: item.draft_slot)
            }
            history = list(session.scalars(
                select(RecommendationHistory)
                .where(RecommendationHistory.draft_id == draft_id)
                .order_by(RecommendationHistory.id)
            ))
            return {
                "export_schema_version": 1,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "draft": {
                    "id": draft.id,
                    "name": draft.name,
                    "season": draft.season,
                    "kind": draft.draft_kind,
                    "status": draft.status,
                    "created_at": _iso(draft.created_at),
                    "completed_at": _iso(draft.completed_at),
                    "archived_at": _iso(draft.archived_at),
                },
                "configuration_snapshot": yaml.safe_load(draft.config_snapshot),
                "draft_order": [
                    {
                        "team_id": team.team_id,
                        "name": team.name,
                        "draft_slot": team.draft_slot,
                        "is_user": team.is_user,
                    }
                    for team in sorted(draft.teams, key=lambda item: item.draft_slot)
                ],
                "data_snapshot": {
                    key: {"import_run_id": run_id, **runs.get(int(run_id), {})}
                    for key, run_id in (draft.data_snapshot or {}).items()
                },
                "selections": selections,
                "rosters": rosters,
                "recommendation_history": [
                    {
                        "overall_pick": item.overall_pick,
                        "source": item.source,
                        "model": item.model,
                        "candidate_ids": item.candidates,
                        "response": item.response,
                        "latency_ms": item.latency_ms,
                        "created_at": _iso(item.created_at),
                    }
                    for item in history
                ],
            }

    def json_text(self, draft_id: int) -> str:
        return json.dumps(self.json_data(draft_id), indent=2, sort_keys=False)

    def csv_text(self, draft_id: int) -> str:
        data = self.json_data(draft_id)
        teams = {team["team_id"]: team["name"] for team in data["draft_order"]}
        output = io.StringIO()
        fields = [
            "overall_pick", "round", "pick_in_round", "team", "player",
            "position", "nfl_team", "fantasypros_id", "yahoo_id", "timestamp",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for pick in data["selections"]:
            player = pick["player"]
            writer.writerow({
                "overall_pick": pick["overall_pick"],
                "round": pick["round"],
                "pick_in_round": pick["pick_in_round"],
                "team": teams[pick["team_id"]],
                "player": player["name"],
                "position": player["position"],
                "nfl_team": player["nfl_team"] or "",
                "fantasypros_id": player["external_ids"].get("fantasypros", ""),
                "yahoo_id": player["external_ids"].get("yahoo", ""),
                "timestamp": pick["selected_at"],
            })
        return output.getvalue()


class DatabaseBackupService:
    def __init__(self, engine: Engine, backup_dir: Path):
        self.engine = engine
        self.backup_dir = backup_dir

    def create(self, *, label: str = "manual") -> Path:
        if self.engine.dialect.name != "sqlite" or not self.engine.url.database:
            raise RuntimeError("automatic database backup currently supports SQLite only")
        source_path = Path(self.engine.url.database).resolve()
        if not source_path.exists():
            raise RuntimeError("SQLite database file does not exist")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        safe_label = "".join(char for char in label if char.isalnum() or char in "-_") or "backup"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.backup_dir / f"fantasy-draft-{timestamp}-{safe_label}.db"
        with sqlite3.connect(source_path) as source, sqlite3.connect(destination) as target:
            source.backup(target)
        return destination
