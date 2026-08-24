from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from fantasy_draft.config import LeagueConfig, RosterSlot


class DraftRuleError(ValueError):
    """Raised when a requested action violates deterministic draft rules."""


@dataclass(frozen=True)
class PickCoordinates:
    overall: int
    round_number: int
    pick_in_round: int
    draft_slot: int


@dataclass(frozen=True)
class RosterPlayer:
    id: str
    eligible_positions: frozenset[str]


class PickLike(Protocol):
    overall_pick: int
    team_id: str


def pick_coordinates(overall_pick: int, team_count: int) -> PickCoordinates:
    if overall_pick < 1:
        raise DraftRuleError("overall pick must be at least 1")
    if team_count < 2:
        raise DraftRuleError("team count must be at least 2")
    round_number = ((overall_pick - 1) // team_count) + 1
    pick_in_round = ((overall_pick - 1) % team_count) + 1
    draft_slot = pick_in_round if round_number % 2 else team_count - pick_in_round + 1
    return PickCoordinates(overall_pick, round_number, pick_in_round, draft_slot)


def team_for_pick(config: LeagueConfig, overall_pick: int) -> str:
    coordinates = pick_coordinates(overall_pick, config.league.team_count)
    return config.team_by_slot(coordinates.draft_slot).id


def next_pick_for_team(
    config: LeagueConfig, team_id: str, from_overall: int
) -> int | None:
    total_picks = config.league.team_count * config.draft.rounds
    for overall in range(max(1, from_overall), total_picks + 1):
        if team_for_pick(config, overall) == team_id:
            return overall
    return None


def picks_until(next_overall: int | None, current_overall: int) -> int | None:
    if next_overall is None:
        return None
    return max(0, next_overall - current_overall)


def teams_selecting_before(
    config: LeagueConfig, current_overall: int, target_overall: int | None
) -> list[str]:
    if target_overall is None:
        return []
    return [
        team_for_pick(config, overall)
        for overall in range(current_overall, target_overall)
    ]


def expanded_roster_slots(slots: Iterable[RosterSlot]) -> list[tuple[str, frozenset[str]]]:
    expanded: list[tuple[str, frozenset[str]]] = []
    for slot in slots:
        if not slot.draftable:
            continue
        for number in range(1, slot.count + 1):
            suffix = str(number) if slot.count > 1 else ""
            expanded.append((f"{slot.code}{suffix}", frozenset(slot.eligible_positions)))
    return expanded


def assign_roster(
    players: Iterable[RosterPlayer], slots: Iterable[RosterSlot]
) -> dict[str, str] | None:
    """Return player-to-slot assignments, or None if no legal assignment exists.

    A bipartite match correctly handles overlapping flex and bench eligibility without
    encoding any particular league's position rules in Python.
    """
    player_list = list(players)
    roster_slots = expanded_roster_slots(slots)
    if len(player_list) > len(roster_slots):
        return None

    # Match constrained players first. Bench/flex naturally sort behind specific slots.
    player_list.sort(key=lambda player: sum(
        bool(player.eligible_positions & eligible) for _, eligible in roster_slots
    ))
    occupied: dict[int, str] = {}

    def place(player: RosterPlayer, visited: set[int]) -> bool:
        candidates = sorted(
            (
                (index, len(eligible), name)
                for index, (name, eligible) in enumerate(roster_slots)
                if player.eligible_positions & eligible
            ),
            key=lambda candidate: (candidate[1], candidate[2]),
        )
        for index, _, _ in candidates:
            if index in visited:
                continue
            visited.add(index)
            previous_id = occupied.get(index)
            if previous_id is None:
                occupied[index] = player.id
                return True
            previous = next(item for item in player_list if item.id == previous_id)
            if place(previous, visited):
                occupied[index] = player.id
                return True
        return False

    for player in player_list:
        if not place(player, set()):
            return None

    return {player_id: roster_slots[index][0] for index, player_id in occupied.items()}


def validate_roster(players: Iterable[RosterPlayer], config: LeagueConfig) -> dict[str, str]:
    assignment = assign_roster(players, config.roster.slots)
    if assignment is None:
        raise DraftRuleError("player does not fit in the team's remaining roster slots")
    return assignment


def remaining_starting_needs(
    assignment: dict[str, str], config: LeagueConfig
) -> list[str]:
    """List unfilled non-bench draftable slots using the best legal assignment."""
    occupied = set(assignment.values())
    needs: list[str] = []
    for slot in config.roster.slots:
        if not slot.starter:
            continue
        for number in range(1, slot.count + 1):
            suffix = str(number) if slot.count > 1 else ""
            if f"{slot.code}{suffix}" not in occupied:
                needs.append(slot.code)
    return needs


def remaining_roster_slots(
    assignment: dict[str, str], config: LeagueConfig
) -> list[str]:
    """List every unfilled draftable roster slot from league configuration."""
    occupied = set(assignment.values())
    remaining: list[str] = []
    for slot in config.roster.slots:
        if not slot.draftable:
            continue
        for number in range(1, slot.count + 1):
            suffix = str(number) if slot.count > 1 else ""
            if f"{slot.code}{suffix}" not in occupied:
                remaining.append(slot.code)
    return remaining
