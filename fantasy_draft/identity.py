from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from fantasy_draft.models import Player, PlayerExternalId


ID_PRIORITY = ("fantasypros", "yahoo", "gsis", "nfl", "espn", "sleeper", "cbs", "sportsdata")


def clean_external_id(value) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return None if not cleaned or cleaned.upper() in {"NA", "N/A", "NULL", "NONE"} else cleaned


def normalize_name(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    tokens = re.sub(r"[^a-z0-9 ]", " ", ascii_name.lower()).split()
    while tokens and tokens[-1] in {"jr", "sr", "ii", "iii", "iv", "v"}:
        tokens.pop()
    return " ".join(tokens)


def fantasypros_external_ids(record: dict) -> dict[str, str]:
    aliases = {
        "fantasypros": ("player_id", "fpid"),
        "yahoo": ("player_yahoo_id", "yahoo_id", "rank_yahoo_id"),
        "espn": ("player_espn_id", "espn_id"),
        "cbs": ("cbs_player_id", "player_cbs_id", "cbs_id"),
        "nfl": ("player_nfl_id", "nfl_id"),
        "gsis": ("gsis_id",),
        "sportsdata": ("sportsdata_player_id", "sportsdata_id"),
    }
    result: dict[str, str] = {}
    for provider, fields in aliases.items():
        for field in fields:
            value = clean_external_id(record.get(field))
            if value:
                result[provider] = value
                break
    return result


@dataclass(frozen=True)
class MatchResult:
    player: Player | None
    method: str
    ambiguous: bool = False


class PlayerIdentityService:
    def match(
        self,
        session: Session,
        *,
        external_ids: dict[str, str],
        name: str | None,
        position: str | None,
        team: str | None,
    ) -> MatchResult:
        exact_players: dict[str, Player] = {}
        for provider in ID_PRIORITY:
            external_id = external_ids.get(provider)
            if not external_id:
                continue
            record = session.scalar(
                select(PlayerExternalId).where(
                    PlayerExternalId.provider == provider,
                    PlayerExternalId.external_id == external_id,
                )
            )
            if record:
                player = session.get(Player, record.player_id)
                exact_players[player.id] = player
        if len(exact_players) == 1:
            return MatchResult(next(iter(exact_players.values())), "exact-id")
        if len(exact_players) > 1:
            return MatchResult(None, "conflicting-exact-ids", True)
        if not name or not position:
            return MatchResult(None, "insufficient-fields")
        candidates = list(session.scalars(select(Player).where(Player.primary_position == position)))
        normalized = normalize_name(name)
        matches = [
            player for player in candidates
            if normalize_name(player.name) == normalized
            and (not team or not player.nfl_team or player.nfl_team == team)
        ]
        if len(matches) == 1:
            return MatchResult(matches[0], "name-position-team")
        return MatchResult(None, "ambiguous-name" if len(matches) > 1 else "no-match", len(matches) > 1)

    def create_player(self, session: Session, *, name: str, position: str, team: str | None) -> Player:
        player = Player(
            id=f"player-{uuid4()}", name=name, nfl_team=team,
            primary_position=position, eligible_positions=[position], active=True,
        )
        session.add(player)
        session.flush()
        return player

    def attach_ids(
        self, session: Session, player: Player, external_ids: dict[str, str], source: str
    ) -> None:
        mirror = dict(player.external_ids or {})
        validated: list[tuple[str, str, PlayerExternalId | None]] = []
        for provider, external_id in external_ids.items():
            existing = session.scalar(
                select(PlayerExternalId).where(
                    PlayerExternalId.provider == provider,
                    PlayerExternalId.external_id == external_id,
                )
            )
            if existing and existing.player_id != player.id:
                raise ValueError(f"external {provider} id is already assigned")
            own = session.scalar(
                select(PlayerExternalId).where(
                    PlayerExternalId.player_id == player.id,
                    PlayerExternalId.provider == provider,
                )
            )
            if own is None:
                pass
            elif own.external_id != external_id:
                raise ValueError(f"player already has a different {provider} id")
            validated.append((provider, external_id, own))
        for provider, external_id, own in validated:
            if own is None:
                session.add(PlayerExternalId(
                    player_id=player.id, provider=provider, external_id=external_id,
                    source=source, verified=True,
                ))
            mirror[provider] = external_id
        player.external_ids = mirror
