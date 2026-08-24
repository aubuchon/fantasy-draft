from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from fantasy_draft.config import (
    LeagueConfig,
    dump_league_config,
    load_league_config_text,
)
from fantasy_draft.engine import (
    DraftRuleError,
    PickCoordinates,
    RosterPlayer,
    next_pick_for_team,
    pick_coordinates,
    remaining_roster_slots,
    remaining_starting_needs,
    team_for_pick,
    teams_selecting_before,
    validate_roster,
)
from fantasy_draft.models import Draft, DraftPick, DraftTeam, Player


class DraftNotFoundError(LookupError):
    pass


class PlayerNotFoundError(LookupError):
    pass


class DraftConflictError(DraftRuleError):
    pass


@dataclass(frozen=True)
class DraftState:
    draft_id: int
    config: LeagueConfig
    picks: list[DraftPick]
    available_players: list[Player]
    current_pick: PickCoordinates | None
    current_team_id: str | None
    next_user_pick: int | None
    picks_until_user_pick: int | None
    teams_before_user_pick: list[str]
    roster_assignments: dict[str, dict[str, str]]
    team_needs: dict[str, list[str]]
    team_remaining_slots: dict[str, list[str]]

    @property
    def complete(self) -> bool:
        return self.current_pick is None


class DraftService:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def create_draft(self, config: LeagueConfig) -> int:
        with self.session_factory.begin() as session:
            draft = Draft(
                name=config.league.name,
                config_snapshot=dump_league_config(config),
                status="active",
            )
            session.add(draft)
            session.flush()
            session.add_all(
                DraftTeam(
                    draft_id=draft.id,
                    team_id=team.id,
                    name=team.name,
                    draft_slot=team.draft_slot,
                    is_user=team.id == config.draft.our_team_id,
                )
                for team in config.teams
            )
            return draft.id

    def get_or_create_active_draft(self, config: LeagueConfig) -> int:
        with self.session_factory() as session:
            draft_id = session.scalar(
                select(Draft.id).where(Draft.status == "active").order_by(Draft.id.desc())
            )
        return draft_id if draft_id is not None else self.create_draft(config)

    def _get_draft(self, session: Session, draft_id: int) -> Draft:
        draft = session.get(Draft, draft_id)
        if draft is None:
            raise DraftNotFoundError(f"draft {draft_id} does not exist")
        return draft

    def _team_roster_players(
        self,
        session: Session,
        draft_id: int,
        team_id: str,
        excluding_overall: int | None = None,
    ) -> list[RosterPlayer]:
        query = (
            select(Player)
            .join(DraftPick, DraftPick.player_id == Player.id)
            .where(DraftPick.draft_id == draft_id, DraftPick.team_id == team_id)
        )
        if excluding_overall is not None:
            query = query.where(DraftPick.overall_pick != excluding_overall)
        return [
            RosterPlayer(player.id, frozenset(player.eligible_positions))
            for player in session.scalars(query)
        ]

    def make_pick(self, draft_id: int, player_id: str) -> int:
        try:
            with self.session_factory.begin() as session:
                draft = self._get_draft(session, draft_id)
                config = load_league_config_text(draft.config_snapshot)
                completed = session.scalar(
                    select(func.count(DraftPick.id)).where(DraftPick.draft_id == draft_id)
                ) or 0
                overall = completed + 1
                total = config.league.team_count * config.draft.rounds
                if overall > total:
                    raise DraftConflictError("the draft is already complete")
                player = session.get(Player, player_id)
                if player is None or not player.active:
                    raise PlayerNotFoundError(f"active player {player_id!r} does not exist")
                already_drafted = session.scalar(
                    select(DraftPick.id).where(
                        DraftPick.draft_id == draft_id,
                        DraftPick.player_id == player_id,
                    )
                )
                if already_drafted is not None:
                    raise DraftConflictError(f"{player.name} has already been drafted")

                coordinates = pick_coordinates(overall, config.league.team_count)
                team_id = team_for_pick(config, overall)
                roster = self._team_roster_players(session, draft_id, team_id)
                roster.append(RosterPlayer(player.id, frozenset(player.eligible_positions)))
                validate_roster(roster, config)
                session.add(
                    DraftPick(
                        draft_id=draft_id,
                        overall_pick=overall,
                        round_number=coordinates.round_number,
                        pick_in_round=coordinates.pick_in_round,
                        team_id=team_id,
                        player_id=player.id,
                    )
                )
                return overall
        except IntegrityError as exc:
            raise DraftConflictError("pick conflicted with another saved selection") from exc

    def undo_last_pick(self, draft_id: int) -> DraftPick:
        with self.session_factory.begin() as session:
            self._get_draft(session, draft_id)
            pick = session.scalar(
                select(DraftPick)
                .where(DraftPick.draft_id == draft_id)
                .order_by(DraftPick.overall_pick.desc())
                .limit(1)
            )
            if pick is None:
                raise DraftConflictError("there are no picks to undo")
            # Materialize fields before detaching the deleted row.
            _ = pick.player
            session.delete(pick)
            return pick

    def correct_pick(self, draft_id: int, overall_pick: int, player_id: str) -> None:
        try:
            with self.session_factory.begin() as session:
                draft = self._get_draft(session, draft_id)
                config = load_league_config_text(draft.config_snapshot)
                pick = session.scalar(
                    select(DraftPick).where(
                        DraftPick.draft_id == draft_id,
                        DraftPick.overall_pick == overall_pick,
                    )
                )
                if pick is None:
                    raise DraftConflictError(f"pick {overall_pick} has not been made")
                player = session.get(Player, player_id)
                if player is None or not player.active:
                    raise PlayerNotFoundError(f"active player {player_id!r} does not exist")
                used_elsewhere = session.scalar(
                    select(DraftPick.id).where(
                        DraftPick.draft_id == draft_id,
                        DraftPick.player_id == player_id,
                        DraftPick.overall_pick != overall_pick,
                    )
                )
                if used_elsewhere is not None:
                    raise DraftConflictError(f"{player.name} has already been drafted")
                roster = self._team_roster_players(
                    session, draft_id, pick.team_id, excluding_overall=overall_pick
                )
                roster.append(RosterPlayer(player.id, frozenset(player.eligible_positions)))
                validate_roster(roster, config)
                pick.player_id = player_id
        except IntegrityError as exc:
            raise DraftConflictError("correction conflicted with a saved selection") from exc

    def get_state(
        self,
        draft_id: int,
        *,
        search: str = "",
        position: str = "",
        sort: str = "rank",
    ) -> DraftState:
        with self.session_factory() as session:
            draft = self._get_draft(session, draft_id)
            config = load_league_config_text(draft.config_snapshot)
            picks = list(
                session.scalars(
                    select(DraftPick)
                    .where(DraftPick.draft_id == draft_id)
                    .order_by(DraftPick.overall_pick)
                )
            )
            for pick in picks:
                _ = pick.player

            drafted_ids = select(DraftPick.player_id).where(DraftPick.draft_id == draft_id)
            available_query = select(Player).where(
                Player.active.is_(True), Player.id.not_in(drafted_ids)
            )
            if search:
                available_query = available_query.where(Player.name.ilike(f"%{search.strip()}%"))
            if position:
                available_query = available_query.where(Player.primary_position == position.upper())
            sort_columns = {
                "rank": (Player.overall_rank.asc().nullslast(), Player.name),
                "adp": (Player.adp.asc().nullslast(), Player.name),
                "tier": (Player.tier.asc().nullslast(), Player.overall_rank.asc().nullslast()),
                "projection": (
                    Player.projected_points.desc().nullslast(),
                    Player.overall_rank.asc().nullslast(),
                ),
            }
            available = list(session.scalars(available_query.order_by(*sort_columns.get(sort, sort_columns["rank"]))))

            total = config.league.team_count * config.draft.rounds
            current_overall = len(picks) + 1
            current = (
                pick_coordinates(current_overall, config.league.team_count)
                if current_overall <= total
                else None
            )
            current_team_id = team_for_pick(config, current_overall) if current else None
            if current:
                next_search_from = (
                    current_overall + 1
                    if current_team_id == config.draft.our_team_id
                    else current_overall
                )
                next_user = next_pick_for_team(
                    config, config.draft.our_team_id, next_search_from
                )
                teams_before = teams_selecting_before(
                    config, next_search_from, next_user
                )
            else:
                next_user = None
                teams_before = []

            assignments: dict[str, dict[str, str]] = {}
            team_needs: dict[str, list[str]] = {}
            team_remaining_slots: dict[str, list[str]] = {}
            for team in config.teams:
                roster = [
                    RosterPlayer(pick.player.id, frozenset(pick.player.eligible_positions))
                    for pick in picks
                    if pick.team_id == team.id
                ]
                assignments[team.id] = validate_roster(roster, config)
                team_needs[team.id] = remaining_starting_needs(assignments[team.id], config)
                team_remaining_slots[team.id] = remaining_roster_slots(
                    assignments[team.id], config
                )

            session.expunge_all()
            return DraftState(
                draft_id=draft_id,
                config=config,
                picks=picks,
                available_players=available,
                current_pick=current,
                current_team_id=current_team_id,
                next_user_pick=next_user,
                picks_until_user_pick=len(teams_before) if next_user is not None else None,
                teams_before_user_pick=teams_before,
                roster_assignments=assignments,
                team_needs=team_needs,
                team_remaining_slots=team_remaining_slots,
            )
