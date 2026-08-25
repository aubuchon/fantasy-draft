from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from fantasy_draft.models import DraftPick, Player, PlayerExternalId


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


@dataclass(frozen=True)
class ReconciliationResult:
    retired_players: tuple[str, ...] = ()
    repointed_picks: int = 0
    conflicts: tuple[str, ...] = ()


def active_identity_duplicates(session: Session) -> dict[tuple[str, str], list[Player]]:
    grouped: dict[tuple[str, str], list[Player]] = defaultdict(list)
    for player in session.scalars(select(Player).where(Player.active.is_(True))):
        grouped[(normalize_name(player.name), player.primary_position)].append(player)
    return {identity: players for identity, players in grouped.items() if len(players) > 1}


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
        candidates = list(session.scalars(select(Player).where(
            Player.primary_position == position,
            Player.active.is_(True),
        )))
        normalized = normalize_name(name)
        name_matches = [
            player for player in candidates
            if normalize_name(player.name) == normalized
        ]
        if len(name_matches) == 1:
            player = name_matches[0]
            same_team = not team or not player.nfl_team or player.nfl_team == team
            providers = set((player.external_ids or {}).keys())
            sample_fallback = player.id.startswith("sample-") or providers == {"sample"}
            if same_team:
                return MatchResult(player, "name-position-team")
            if sample_fallback:
                return MatchResult(player, "name-position-team-change")
            return MatchResult(None, "team-conflict")
        team_matches = [
            player for player in name_matches
            if not team or not player.nfl_team or player.nfl_team == team
        ]
        if len(team_matches) == 1:
            return MatchResult(team_matches[0], "name-position-team")
        ambiguous = len(name_matches) > 1
        return MatchResult(None, "ambiguous-name" if ambiguous else "no-match", ambiguous)

    def reconcile_authoritative_duplicates(self, session: Session) -> ReconciliationResult:
        """Retire only safe sample fallbacks superseded by one provider-backed identity."""
        providers_by_player: dict[str, set[str]] = defaultdict(set)
        for external in session.scalars(select(PlayerExternalId)):
            providers_by_player[external.player_id].add(external.provider)

        retired: list[str] = []
        conflicts: list[str] = []
        repointed = 0
        for identity, players in active_identity_duplicates(session).items():
            def providers(player: Player) -> set[str]:
                return providers_by_player[player.id] | set((player.external_ids or {}).keys())

            authoritative = [
                player for player in players if providers(player).intersection(ID_PRIORITY)
            ]
            fallbacks = [
                player for player in players
                if player not in authoritative
                and (player.id.startswith("sample-") or providers(player) == {"sample"})
            ]
            if len(authoritative) != 1:
                continue
            canonical = authoritative[0]
            canonical_drafts = set(session.scalars(
                select(DraftPick.draft_id).where(DraftPick.player_id == canonical.id)
            ))
            for fallback in fallbacks:
                fallback_drafts = set(session.scalars(
                    select(DraftPick.draft_id).where(DraftPick.player_id == fallback.id)
                ))
                overlapping = canonical_drafts.intersection(fallback_drafts)
                if overlapping:
                    conflicts.append(
                        f"{identity[0]} ({identity[1]}) is duplicated in draft(s) "
                        + ", ".join(str(value) for value in sorted(overlapping))
                    )
                    continue
                result = session.execute(
                    update(DraftPick)
                    .where(DraftPick.player_id == fallback.id)
                    .values(player_id=canonical.id)
                )
                repointed += result.rowcount or 0
                fallback.active = False
                retired.append(fallback.id)
                canonical_drafts.update(fallback_drafts)
        return ReconciliationResult(tuple(retired), repointed, tuple(conflicts))

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
