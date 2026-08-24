from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fantasy_draft.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Player(Base):
    __tablename__ = "players"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    nfl_team: Mapped[str | None] = mapped_column(String(10), nullable=True)
    primary_position: Mapped[str] = mapped_column(String(10), index=True)
    eligible_positions: Mapped[list[str]] = mapped_column(JSON, default=list)
    external_ids: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    overall_rank: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    position_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    adp: Mapped[float | None] = mapped_column(Float, nullable=True)
    tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    projected_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    floor: Mapped[float | None] = mapped_column(Float, nullable=True)
    ceiling: Mapped[float | None] = mapped_column(Float, nullable=True)
    upside: Mapped[float | None] = mapped_column(Float, nullable=True)
    role_certainty: Mapped[float | None] = mapped_column(Float, nullable=True)
    injury_risk: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    external_id_records: Mapped[list["PlayerExternalId"]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )


class PlayerExternalId(Base):
    __tablename__ = "player_external_ids"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_external_provider_id"),
        UniqueConstraint("player_id", "provider", name="uq_player_provider"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(120), index=True)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    player: Mapped[Player] = relationship(back_populates="external_id_records")


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draft_kind: Mapped[str] = mapped_column(String(20), default="practice", index=True)
    config_snapshot: Mapped[str] = mapped_column(Text)
    data_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    teams: Mapped[list["DraftTeam"]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )
    picks: Mapped[list["DraftPick"]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )


class DraftTeam(Base):
    __tablename__ = "draft_teams"
    __table_args__ = (
        UniqueConstraint("draft_id", "team_id", name="uq_draft_team_id"),
        UniqueConstraint("draft_id", "draft_slot", name="uq_draft_team_slot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id", ondelete="CASCADE"))
    team_id: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(120))
    draft_slot: Mapped[int] = mapped_column(Integer)
    is_user: Mapped[bool] = mapped_column(Boolean, default=False)

    draft: Mapped[Draft] = relationship(back_populates="teams")


class DraftPick(Base):
    __tablename__ = "draft_picks"
    __table_args__ = (
        UniqueConstraint("draft_id", "overall_pick", name="uq_draft_overall_pick"),
        UniqueConstraint("draft_id", "player_id", name="uq_draft_player"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id", ondelete="CASCADE"))
    overall_pick: Mapped[int] = mapped_column(Integer)
    round_number: Mapped[int] = mapped_column(Integer)
    pick_in_round: Mapped[int] = mapped_column(Integer)
    team_id: Mapped[str] = mapped_column(String(80), index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    market_adp: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_rank: Mapped[float | None] = mapped_column(Float, nullable=True)

    draft: Mapped[Draft] = relationship(back_populates="picks")
    player: Mapped[Player] = relationship()


class ApplicationState(Base):
    __tablename__ = "application_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    current_draft_id: Mapped[int | None] = mapped_column(
        ForeignKey("drafts.id", ondelete="SET NULL"), nullable=True
    )


class ImportRun(Base):
    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    dataset: Mapped[str] = mapped_column(String(40), index=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    data_mode: Mapped[str] = mapped_column(String(20), default="unknown")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    records_received: Mapped[int] = mapped_column(Integer, default=0)
    records_matched: Mapped[int] = mapped_column(Integer, default=0)
    records_created: Mapped[int] = mapped_column(Integer, default=0)
    records_unmatched: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    raw_cache_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)


class UnmatchedRecord(Base):
    __tablename__ = "unmatched_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_run_id: Mapped[int] = mapped_column(ForeignKey("import_runs.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    position: Mapped[str | None] = mapped_column(String(20), nullable=True)
    team: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str] = mapped_column(String(200))
    raw_record: Mapped[dict] = mapped_column(JSON, default=dict)
    resolved_player_id: Mapped[str | None] = mapped_column(
        ForeignKey("players.id", ondelete="SET NULL"), nullable=True
    )


class PlayerRanking(Base):
    __tablename__ = "player_rankings"
    __table_args__ = (
        UniqueConstraint(
            "import_run_id", "player_id", "ranking_type", "scoring",
            name="uq_import_player_ranking",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    import_run_id: Mapped[int] = mapped_column(ForeignKey("import_runs.id", ondelete="CASCADE"), index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    season: Mapped[int] = mapped_column(Integer, index=True)
    ranking_type: Mapped[str] = mapped_column(String(30), index=True)
    scoring: Mapped[str | None] = mapped_column(String(20), nullable=True)
    overall_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_rank: Mapped[str | None] = mapped_column(String(20), nullable=True)
    adp: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    worst_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    mean_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank_stddev: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_record: Mapped[dict] = mapped_column(JSON, default=dict)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PlayerProjection(Base):
    __tablename__ = "player_projections"
    __table_args__ = (
        UniqueConstraint("import_run_id", "player_id", name="uq_import_player_projection"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    import_run_id: Mapped[int] = mapped_column(ForeignKey("import_runs.id", ondelete="CASCADE"), index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    season: Mapped[int] = mapped_column(Integer, index=True)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    projection_type: Mapped[str] = mapped_column(String(30), default="preseason")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    provider_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RecommendationHistory(Base):
    __tablename__ = "recommendation_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id", ondelete="CASCADE"), index=True)
    overall_pick: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(60))
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    candidates: Mapped[list] = mapped_column(JSON, default=list)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
