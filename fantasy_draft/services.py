from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from fantasy_draft.config import (
    LeagueConfig,
    StrategyPreferences,
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
from fantasy_draft.models import (
    ApplicationState,
    Draft,
    DraftPick,
    DraftTeam,
    ImportRun,
    Player,
    utc_now,
)


class DraftNotFoundError(LookupError):
    pass


class PlayerNotFoundError(LookupError):
    pass


class DraftConflictError(DraftRuleError):
    pass


@dataclass(frozen=True)
class DraftState:
    draft_id: int
    draft_name: str
    draft_status: str
    draft_kind: str
    season: int | None
    data_snapshot: dict[str, int]
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

    def _set_current(self, session: Session, draft_id: int | None) -> None:
        app_state = session.get(ApplicationState, 1)
        if app_state is None:
            session.add(ApplicationState(id=1, current_draft_id=draft_id))
        else:
            app_state.current_draft_id = draft_id

    def _latest_data_snapshot(self, session: Session, season: int) -> dict[str, int]:
        snapshot: dict[str, int] = {}
        runs = session.scalars(
            select(ImportRun)
            .where(ImportRun.status.in_(["success", "cached"]))
            .order_by(ImportRun.completed_at.desc(), ImportRun.id.desc())
        )
        for run in runs:
            if run.dataset in {"rankings", "adp", "projections"} and run.season != season:
                continue
            snapshot.setdefault(f"{run.provider}:{run.dataset}", run.id)
        return snapshot

    def _add_draft(
        self,
        session: Session,
        config: LeagueConfig,
        *,
        name: str,
        season: int,
        draft_kind: str,
        status: str,
        data_snapshot: dict | None = None,
    ) -> Draft:
        draft = Draft(
            name=name,
            season=season,
            draft_kind=draft_kind,
            config_snapshot=dump_league_config(config),
            data_snapshot=data_snapshot if data_snapshot is not None else self._latest_data_snapshot(session, season),
            status=status,
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
        return draft

    def create_draft(
        self,
        config: LeagueConfig,
        *,
        name: str | None = None,
        season: int | None = None,
        draft_kind: str = "practice",
        status: str = "active",
        select_current: bool = True,
    ) -> int:
        if draft_kind not in {"practice", "live"}:
            raise DraftConflictError("draft kind must be practice or live")
        if status not in {"setup", "active"}:
            raise DraftConflictError("new drafts must start in setup or active state")
        with self.session_factory.begin() as session:
            draft = self._add_draft(
                session,
                config,
                name=name or config.league.name,
                season=season or date.today().year,
                draft_kind=draft_kind,
                status=status,
            )
            if select_current:
                self._set_current(session, draft.id)
            return draft.id

    def current_draft_id(self) -> int | None:
        with self.session_factory() as session:
            state = session.get(ApplicationState, 1)
            return state.current_draft_id if state else None

    def list_drafts(self) -> list[Draft]:
        with self.session_factory() as session:
            drafts = list(session.scalars(select(Draft).order_by(Draft.created_at.desc(), Draft.id.desc())))
            session.expunge_all()
            return drafts

    def switch_draft(self, draft_id: int) -> None:
        with self.session_factory.begin() as session:
            self._get_draft(session, draft_id)
            self._set_current(session, draft_id)

    def update_strategy_preferences(
        self, draft_id: int, preferences: StrategyPreferences
    ) -> None:
        """Explicitly update advisory preferences without changing league rules or picks."""
        with self.session_factory.begin() as session:
            draft = self._get_draft(session, draft_id)
            if draft.status not in {"setup", "active"}:
                raise DraftConflictError(
                    "strategy preferences can only change on setup or active drafts"
                )
            config = load_league_config_text(draft.config_snapshot)
            raw = config.model_dump()
            raw["strategy"] = preferences.model_dump()
            draft.config_snapshot = dump_league_config(
                LeagueConfig.model_validate(raw)
            )
            draft.updated_at = utc_now()

    def activate_draft(self, draft_id: int) -> None:
        with self.session_factory.begin() as session:
            draft = self._get_draft(session, draft_id)
            if draft.status != "setup":
                raise DraftConflictError("only a setup draft can be activated")
            draft.status = "active"
            self._set_current(session, draft_id)

    def archive_draft(self, draft_id: int) -> None:
        with self.session_factory.begin() as session:
            draft = self._get_draft(session, draft_id)
            if draft.status == "archived":
                return
            draft.status = "archived"
            draft.archived_at = utc_now()

    def reset_draft(self, draft_id: int) -> int:
        """Archive a draft and create an empty active rehearsal from its snapshots."""
        with self.session_factory.begin() as session:
            original = self._get_draft(session, draft_id)
            config = load_league_config_text(original.config_snapshot)
            original.status = "archived"
            original.archived_at = utc_now()
            replacement = self._add_draft(
                session,
                config,
                name=f"{original.name} — reset",
                season=original.season or date.today().year,
                draft_kind=original.draft_kind,
                status="active",
                data_snapshot=dict(original.data_snapshot or {}),
            )
            self._set_current(session, replacement.id)
            return replacement.id

    def attach_data_snapshot(self, draft_id: int, import_run_ids: list[int]) -> None:
        """Pin successful refreshed datasets to an empty draft; never revalue picks retroactively."""
        with self.session_factory.begin() as session:
            draft = self._get_draft(session, draft_id)
            if session.scalar(
                select(func.count(DraftPick.id)).where(DraftPick.draft_id == draft_id)
            ):
                raise DraftConflictError(
                    "data can only be pinned before the first pick; create a new draft to use new data"
                )
            runs = list(session.scalars(select(ImportRun).where(ImportRun.id.in_(import_run_ids))))
            snapshot = dict(draft.data_snapshot or {})
            for run in runs:
                if run.status in {"success", "cached"}:
                    snapshot[f"{run.provider}:{run.dataset}"] = run.id
            draft.data_snapshot = snapshot

    def get_or_create_active_draft(self, config: LeagueConfig) -> int:
        current = self.current_draft_id()
        if current is not None:
            return current
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

    def _validate_user_position_limit(
        self,
        session: Session,
        draft_id: int,
        team_id: str,
        player: Player,
        config: LeagueConfig,
        *,
        excluding_overall: int | None = None,
    ) -> None:
        maximum = config.strategy.max_roster_counts.get(player.primary_position)
        if team_id != config.draft.our_team_id or maximum is None:
            return
        query = (
            select(func.count(DraftPick.id))
            .join(Player, DraftPick.player_id == Player.id)
            .where(
                DraftPick.draft_id == draft_id,
                DraftPick.team_id == team_id,
                Player.primary_position == player.primary_position,
            )
        )
        if excluding_overall is not None:
            query = query.where(DraftPick.overall_pick != excluding_overall)
        current = session.scalar(query) or 0
        if current >= maximum:
            raise DraftConflictError(
                f"strategy limits our roster to {maximum} {player.primary_position}"
            )

    def make_pick(self, draft_id: int, player_id: str) -> int:
        try:
            with self.session_factory.begin() as session:
                draft = self._get_draft(session, draft_id)
                if draft.status != "active":
                    raise DraftConflictError("this draft is read-only until it is active")
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
                self._validate_user_position_limit(
                    session, draft_id, team_id, player, config
                )
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
                        market_adp=player.adp,
                        market_rank=float(player.overall_rank) if player.overall_rank else None,
                    )
                )
                if overall == total:
                    draft.status = "completed"
                    draft.completed_at = utc_now()
                return overall
        except IntegrityError as exc:
            raise DraftConflictError("pick conflicted with another saved selection") from exc

    def undo_last_pick(self, draft_id: int) -> DraftPick:
        with self.session_factory.begin() as session:
            draft = self._get_draft(session, draft_id)
            if draft.status not in {"active", "completed"}:
                raise DraftConflictError("this draft is read-only")
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
            if draft.status == "completed":
                draft.status = "active"
                draft.completed_at = None
            return pick

    def correct_pick(self, draft_id: int, overall_pick: int, player_id: str) -> None:
        try:
            with self.session_factory.begin() as session:
                draft = self._get_draft(session, draft_id)
                if draft.status != "active":
                    raise DraftConflictError("this draft is read-only")
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
                self._validate_user_position_limit(
                    session,
                    draft_id,
                    pick.team_id,
                    player,
                    config,
                    excluding_overall=overall_pick,
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
                draft_name=draft.name,
                draft_status=draft.status,
                draft_kind=draft.draft_kind,
                season=draft.season,
                data_snapshot=dict(draft.data_snapshot or {}),
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
