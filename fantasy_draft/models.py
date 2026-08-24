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
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    config_snapshot: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

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

    draft: Mapped[Draft] = relationship(back_populates="picks")
    player: Mapped[Player] = relationship()

