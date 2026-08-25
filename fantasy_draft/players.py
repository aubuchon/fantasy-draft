from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from fantasy_draft.models import Player


OPTIONAL_FLOAT_FIELDS = (
    "adp",
    "projected_points",
    "floor",
    "ceiling",
    "upside",
    "role_certainty",
    "injury_risk",
)
OPTIONAL_INT_FIELDS = ("overall_rank", "position_rank", "tier", "draft_year")


def _optional_number(row: dict[str, str], field: str, converter):
    value = row.get(field, "").strip()
    return converter(value) if value else None


def import_players(session: Session, path: str | Path) -> int:
    """Insert or update players from a provider-neutral CSV file."""
    count = 0
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            player_id = row["id"].strip()
            player = session.get(Player, player_id)
            if player is None:
                player = Player(id=player_id, name="", primary_position="")
                session.add(player)
            player.name = row["name"].strip()
            player.nfl_team = row.get("nfl_team", "").strip() or None
            birth_date = row.get("birth_date", "").strip()
            player.birth_date = date.fromisoformat(birth_date) if birth_date else None
            player.primary_position = row["position"].strip().upper()
            eligible = row.get("eligible_positions", "").strip()
            player.eligible_positions = (
                [value.strip().upper() for value in eligible.split("|") if value.strip()]
                or [player.primary_position]
            )
            for field in OPTIONAL_INT_FIELDS:
                setattr(player, field, _optional_number(row, field, int))
            for field in OPTIONAL_FLOAT_FIELDS:
                setattr(player, field, _optional_number(row, field, float))
            provider = row.get("provider", "").strip()
            provider_id = row.get("provider_id", "").strip()
            player.external_ids = {provider: provider_id} if provider and provider_id else {}
            player.active = row.get("active", "true").strip().lower() not in {"false", "0", "no"}
            count += 1
    return count


def age_on(birth_date: date | None, as_of: date | None = None) -> int | None:
    if birth_date is None:
        return None
    reference = as_of or date.today()
    if birth_date > reference:
        return None
    return reference.year - birth_date.year - (
        (reference.month, reference.day) < (birth_date.month, birth_date.day)
    )


def experience_label(draft_year: int | None, season: int | None) -> str:
    if draft_year is None or season is None or draft_year > season:
        return "—"
    years = season - draft_year
    return "R" if years == 0 else str(years)


def seed_players_if_empty(session: Session, path: str | Path) -> int:
    if session.scalar(select(Player.id).limit(1)) is not None:
        return 0
    return import_players(session, path)
