from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from fantasy_draft.identity import (
    PlayerIdentityService,
    clean_external_id,
    fantasypros_external_ids,
)
from fantasy_draft.models import (
    ImportRun,
    Player,
    PlayerExternalId,
    PlayerProjection,
    PlayerRanking,
    UnmatchedRecord,
    utc_now,
)
from fantasy_draft.providers import (
    DynastyProcessProvider,
    FantasyProsProvider,
    ProviderError,
    ProviderPayload,
)


logger = logging.getLogger(__name__)


def _float(record: dict, *keys: str) -> float | None:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _int(record: dict, *keys: str) -> int | None:
    value = _float(record, *keys)
    return int(value) if value is not None else None


def _date(record: dict, *keys: str) -> date | None:
    for key in keys:
        value = record.get(key)
        if value in (None, ""):
            continue
        try:
            if isinstance(value, (int, float)) or str(value).isdigit():
                return datetime.fromtimestamp(float(value), tz=timezone.utc).date()
            return date.fromisoformat(str(value)[:10])
        except (ValueError, TypeError, OverflowError, OSError):
            continue
    return None


def detect_data_mode(raw: dict, configured: str) -> str:
    if configured in {"sample", "test", "production"}:
        return "sample" if configured == "test" else configured
    for key in ("data_mode", "mode", "environment"):
        value = str(raw.get(key, "")).lower()
        if value in {"sample", "test"}:
            return "sample"
        if value in {"production", "prod", "live"}:
            return "production"
    if raw.get("test") is True or raw.get("sample") is True:
        return "sample"
    nested = raw.get("responses")
    if isinstance(nested, dict):
        modes = {
            detect_data_mode(value, "auto")
            for value in nested.values() if isinstance(value, dict)
        }
        if "sample" in modes:
            return "sample"
        if modes == {"production"}:
            return "production"
    return "unknown"


def _source_timestamp(raw: dict) -> datetime | None:
    value = raw.get("last_updated") or raw.get("last_update")
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass(frozen=True)
class RefreshResult:
    runs: list[int]
    warnings: list[str]


class DataImportService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        cache_dir: Path,
        *,
        data_mode: str = "auto",
    ):
        self.session_factory = session_factory
        self.cache_dir = cache_dir
        self.data_mode = data_mode
        self.identity = PlayerIdentityService()

    def _cache(self, provider: str, dataset: str, payload: dict | str) -> tuple[str, str]:
        content = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True)
        encoded = content.encode("utf-8")
        checksum = hashlib.sha256(encoded).hexdigest()
        directory = self.cache_dir / provider
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"{timestamp}-{dataset}-{checksum[:10]}.json.gz"
        with gzip.open(path, "wb") as handle:
            handle.write(encoded)
        return str(path), checksum

    def _start_run(
        self, session: Session, provider: str, dataset: str, season: int | None, mode: str
    ) -> ImportRun:
        run = ImportRun(
            provider=provider, dataset=dataset, season=season,
            status="running", data_mode=mode,
        )
        session.add(run)
        session.flush()
        return run

    def _unmatched(self, session: Session, run: ImportRun, record: dict, reason: str) -> None:
        session.add(UnmatchedRecord(
            import_run_id=run.id,
            provider=run.provider,
            external_id=clean_external_id(record.get("player_id") or record.get("fpid")),
            name=record.get("player_name") or record.get("name"),
            position=record.get("player_position_id") or record.get("position_id"),
            team=record.get("player_team_id") or record.get("team_id"),
            reason=reason,
            raw_record=record,
        ))

    def import_players_payload(self, payload: ProviderPayload) -> int:
        path, checksum = self._cache(payload.provider, payload.dataset, payload.raw)
        mode = detect_data_mode(payload.raw, self.data_mode)
        with self.session_factory.begin() as session:
            source_season = _int(payload.raw, "season")
            run = self._start_run(session, payload.provider, payload.dataset, source_season, mode)
            run.raw_cache_path, run.source_checksum = path, checksum
            run.source_updated_at = _source_timestamp(payload.raw)
            run.records_received = len(payload.records)
            for record in payload.records:
                ids = fantasypros_external_ids(record)
                name = record.get("player_name") or record.get("name")
                position = (record.get("position_id") or record.get("player_position_id") or "").upper()
                position = "DEF" if position == "DST" else position
                team = record.get("team_id") or record.get("player_team_id")
                match = self.identity.match(
                    session, external_ids=ids, name=name,
                    position=position or None, team=team,
                )
                if match.ambiguous or not name or not position:
                    self._unmatched(session, run, record, match.method)
                    run.records_unmatched += 1
                    continue
                player = match.player
                if player is None:
                    player = self.identity.create_player(
                        session, name=name, position=position, team=team
                    )
                    run.records_created += 1
                else:
                    run.records_matched += 1
                player.name = name
                player.nfl_team = team or player.nfl_team
                player.primary_position = position
                player.eligible_positions = [position]
                player.active = True
                player.status = record.get("status") or player.status
                player.birth_date = (
                    _date(record, "birthdate", "birth_date", "birthdatetime")
                    or player.birth_date
                )
                player.draft_year = (
                    _int(record, "draft_class", "draft_year") or player.draft_year
                )
                player.overall_rank = _int(record, "rank_ecr", "rank_ecr_ppr") or player.overall_rank
                player.adp = _float(record, "rank_adp", "rank_adp_ppr") or player.adp
                self.identity.attach_ids(session, player, ids, "fantasypros-players")
            run.status = "success"
            run.completed_at = utc_now()
            run.metadata_json = payload.metadata
            return run.id

    def backfill_cached_player_demographics(self) -> int:
        """Normalize age/experience source fields from the latest audited player cache."""
        with self.session_factory() as session:
            run = session.scalar(
                select(ImportRun)
                .where(
                    ImportRun.provider == "fantasypros",
                    ImportRun.dataset == "players",
                    ImportRun.status.in_(["success", "cached"]),
                )
                .order_by(ImportRun.id.desc())
                .limit(1)
            )
            cache_path = Path(run.raw_cache_path) if run and run.raw_cache_path else None
        if cache_path is None or not cache_path.exists():
            return 0
        try:
            with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
                raw = json.load(handle)
            records = list(raw.get("players") or [])
        except (OSError, ValueError, TypeError):
            logger.warning("Could not read cached FantasyPros demographics", exc_info=True)
            return 0

        updated = 0
        with self.session_factory.begin() as session:
            for record in records:
                player = self._fantasypros_player(session, record)
                if player is None:
                    continue
                changed = False
                birth_date = _date(
                    record, "birthdate", "birth_date", "birthdatetime"
                )
                draft_year = _int(record, "draft_class", "draft_year")
                if player.birth_date is None and birth_date is not None:
                    player.birth_date = birth_date
                    changed = True
                if player.draft_year is None and draft_year is not None:
                    player.draft_year = draft_year
                    changed = True
                updated += int(changed)
        return updated

    def _fantasypros_player(self, session: Session, record: dict) -> Player | None:
        fpid = clean_external_id(record.get("player_id") or record.get("fpid"))
        if not fpid:
            return None
        external = session.scalar(select(PlayerExternalId).where(
            PlayerExternalId.provider == "fantasypros",
            PlayerExternalId.external_id == fpid,
        ))
        return session.get(Player, external.player_id) if external else None

    def import_rankings_payload(self, payload: ProviderPayload) -> int:
        path, checksum = self._cache(payload.provider, payload.dataset, payload.raw)
        mode = detect_data_mode(payload.raw, self.data_mode)
        season = int(payload.metadata["season"])
        ranking_type = str(payload.metadata["ranking_type"]).upper()
        scoring = str(payload.metadata["scoring"])
        with self.session_factory.begin() as session:
            run = self._start_run(session, payload.provider, payload.dataset, season, mode)
            run.raw_cache_path, run.source_checksum = path, checksum
            run.week = _int(payload.metadata, "week") or 0
            run.source_updated_at = _source_timestamp(payload.raw)
            run.records_received = len(payload.records)
            run.metadata_json = {**payload.metadata, "source_last_updated": payload.raw.get("last_updated")}
            for record in payload.records:
                player = self._fantasypros_player(session, record)
                if player is None:
                    self._unmatched(session, run, record, "missing-fantasypros-player-id")
                    run.records_unmatched += 1
                    continue
                rank = _float(record, "rank_ecr", "rank_ave", "rank")
                adp = rank if ranking_type == "ADP" else _float(record, "adp", "rank_adp")
                session.add(PlayerRanking(
                    import_run_id=run.id, player_id=player.id, provider=payload.provider,
                    season=season, ranking_type=ranking_type, scoring=scoring,
                    overall_rank=rank if ranking_type != "ADP" else None,
                    position_rank=str(record.get("pos_rank") or "") or None,
                    adp=adp, provider_tier=_int(record, "tier"),
                    best_rank=_float(record, "rank_min", "best_rank"),
                    worst_rank=_float(record, "rank_max", "worst_rank"),
                    mean_rank=_float(record, "rank_ave", "mean_rank"),
                    rank_stddev=_float(record, "rank_std", "rank_stddev"),
                    raw_record=record,
                ))
                if ranking_type == "ADP" and adp is not None:
                    player.adp = adp
                elif rank is not None:
                    player.overall_rank = int(rank)
                    player.tier = _int(record, "tier")
                    pos_rank = str(record.get("pos_rank") or "")
                    digits = "".join(character for character in pos_rank if character.isdigit())
                    player.position_rank = int(digits) if digits else player.position_rank
                run.records_matched += 1
            run.status = "success"
            run.completed_at = utc_now()
            return run.id

    def import_projections_payload(self, payload: ProviderPayload) -> int:
        path, checksum = self._cache(payload.provider, payload.dataset, payload.raw)
        mode = detect_data_mode(payload.raw, self.data_mode)
        season = int(payload.metadata["season"])
        with self.session_factory.begin() as session:
            run = self._start_run(session, payload.provider, payload.dataset, season, mode)
            run.raw_cache_path, run.source_checksum = path, checksum
            run.week = _int(payload.metadata, "week") or 0
            run.source_updated_at = _source_timestamp(payload.raw)
            run.records_received = len(payload.records)
            run.metadata_json = payload.metadata
            for record in payload.records:
                player = self._fantasypros_player(session, record)
                if player is None:
                    self._unmatched(session, run, record, "missing-fantasypros-player-id")
                    run.records_unmatched += 1
                    continue
                raw_stats = record.get("stats") or {}
                if isinstance(raw_stats, dict):
                    stats = dict(raw_stats)
                elif isinstance(raw_stats, list) and all(isinstance(item, dict) for item in raw_stats):
                    stats = {key: value for item in raw_stats for key, value in item.items()}
                else:
                    stats = {}
                session.add(PlayerProjection(
                    import_run_id=run.id, player_id=player.id, provider=payload.provider,
                    season=season, projection_type=str(payload.metadata.get("projection_type", "preseason")),
                    stats=stats, provider_points=_float(stats, "points_ppr", "points"),
                ))
                run.records_matched += 1
            run.status = "success"
            run.completed_at = utc_now()
            return run.id

    def import_dynastyprocess_csv(self, csv_text: str) -> int:
        path, checksum = self._cache("dynastyprocess", "player_ids", csv_text)
        records = list(csv.DictReader(io.StringIO(csv_text)))
        mapping = {
            "fantasypros": "fantasypros_id", "yahoo": "yahoo_id", "gsis": "gsis_id",
            "nfl": "nfl_id", "espn": "espn_id", "sleeper": "sleeper_id",
            "cbs": "cbs_id", "sportsdata": "fantasy_data_id", "mfl": "mfl_id",
            "pfr": "pfr_id", "rotowire": "rotowire_id", "sportradar": "sportradar_id",
        }
        with self.session_factory.begin() as session:
            run = self._start_run(session, "dynastyprocess", "player_ids", None, "production")
            run.raw_cache_path, run.source_checksum = path, checksum
            run.records_received = len(records)
            run.metadata_json = {
                "source": "https://github.com/dynastyprocess/data",
                "license": "GPL-3.0",
            }
            for record in records:
                ids = {
                    provider: value for provider, column in mapping.items()
                    if (value := clean_external_id(record.get(column)))
                }
                position = (record.get("position") or "").upper()
                position = "DEF" if position == "DST" else position
                match = self.identity.match(
                    session, external_ids=ids, name=record.get("name"),
                    position=position or None, team=record.get("team"),
                )
                if match.player is None:
                    run.records_unmatched += 1
                    continue
                try:
                    self.identity.attach_ids(session, match.player, ids, "dynastyprocess-db_playerids")
                    match.player.birth_date = (
                        _date(record, "birthdate") or match.player.birth_date
                    )
                    match.player.draft_year = (
                        _int(record, "draft_year") or match.player.draft_year
                    )
                    run.records_matched += 1
                except ValueError:
                    self._unmatched(session, run, record, "conflicting-crosswalk-id")
                    run.records_unmatched += 1
            run.status = "success"
            run.completed_at = utc_now()
            return run.id

    def refresh_all(
        self,
        provider: FantasyProsProvider,
        crosswalk: DynastyProcessProvider,
        *,
        season: int,
        scoring: str,
        positions: list[str],
    ) -> RefreshResult:
        run_ids: list[int] = []
        warnings: list[str] = []
        operations = [
            ("fantasypros", "players", lambda: self.import_players_payload(provider.get_players())),
            ("dynastyprocess", "player_ids", lambda: self.import_dynastyprocess_csv(crosswalk.fetch_csv())),
            ("fantasypros", "rankings", lambda: self.import_rankings_payload(provider.get_rankings(season, scoring, "DRAFT"))),
            ("fantasypros", "adp", lambda: self.import_rankings_payload(provider.get_rankings(season, scoring, "ADP"))),
            ("fantasypros", "projections", lambda: self.import_projections_payload(provider.get_projections(season, positions))),
        ]
        for provider_name, dataset, operation in operations:
            try:
                run_ids.append(operation())
            except Exception as exc:
                message = str(exc) if isinstance(exc, ProviderError) else f"{dataset} import failed ({type(exc).__name__})"
                warnings.append(message)
                with self.session_factory.begin() as session:
                    run = self._start_run(
                        session, provider_name, dataset,
                        season if provider_name == "fantasypros" and dataset != "players" else None,
                        "unknown",
                    )
                    run.status = "failed"
                    run.completed_at = utc_now()
                    run.errors = [message]
        return RefreshResult(run_ids, warnings)

    def status(self) -> dict[str, Any]:
        with self.session_factory() as session:
            latest: dict[str, ImportRun] = {}
            for run in session.scalars(select(ImportRun).order_by(ImportRun.id.desc())):
                latest.setdefault(f"{run.provider}:{run.dataset}", run)
            unmatched = session.scalar(select(func.count(UnmatchedRecord.id)).where(
                UnmatchedRecord.resolved_player_id.is_(None)
            )) or 0
            external_count = session.scalar(select(func.count(PlayerExternalId.id))) or 0
            return {
                "latest": latest,
                "unmatched": unmatched,
                "external_ids": external_count,
                "players": session.scalar(select(func.count(Player.id))) or 0,
                "ranked": session.scalar(select(func.count(func.distinct(PlayerRanking.player_id)))) or 0,
                "projected": session.scalar(select(func.count(func.distinct(PlayerProjection.player_id)))) or 0,
                "unmatched_records": list(session.scalars(
                    select(UnmatchedRecord)
                    .where(UnmatchedRecord.resolved_player_id.is_(None))
                    .order_by(UnmatchedRecord.id.desc())
                    .limit(100)
                )),
            }
