from __future__ import annotations

import hashlib
import logging
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from typing import Protocol, Sequence

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from fantasy_draft.config import LeagueConfig
from fantasy_draft.identity import normalize_name
from fantasy_draft.models import ImportRun, Player, PlayerProjection, PlayerRanking
from fantasy_draft.scoring import score_projection
from fantasy_draft.services import DraftState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlayerMetrics:
    projected_points: float
    ecr: float | None
    adp: float | None
    provider_tier: int | None
    rank_stddev: float | None
    approximations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplacementProfile:
    demand: dict[str, int]
    levels: dict[str, float]


@dataclass(frozen=True)
class EvaluatedPlayer:
    player: Player
    projected_points: float
    vor: float
    replacement_level: float
    ecr: float | None
    adp: float | None
    provider_tier: int | None
    our_tier: int
    tier_cliff: float
    scarcity: float
    survival_probability: float | None
    cost_of_waiting: float
    roster_fit: float
    upside_adjustment: float
    risk_adjustment: float
    preference_adjustment: float
    quantitative_score: float
    simulations: int
    approximations: tuple[str, ...] = ()


class PlayerEvaluationProvider(Protocol):
    def evaluate(self, state: DraftState, players: Sequence[Player]) -> list[EvaluatedPlayer]: ...


def calculate_replacement_profile(
    config: LeagueConfig,
    projections: dict[str, list[float]],
    drafted_counts: Counter[str] | None = None,
) -> ReplacementProfile:
    """Calculate remaining replacement demand from league slots and drafted players."""
    team_count = config.league.team_count
    drafted_counts = drafted_counts or Counter()
    demand: Counter[str] = Counter()
    flexible: list[set[str]] = []
    bench_slots = 0
    for slot in config.roster.slots:
        if not slot.draftable:
            continue
        eligible = set(slot.eligible_positions)
        total = slot.count * team_count
        if not slot.starter:
            bench_slots += total
        elif len(eligible) == 1:
            demand[next(iter(eligible))] += total
        else:
            flexible.extend([eligible] * total)

    def best_position(eligible: set[str]) -> str | None:
        candidates = []
        for position in eligible:
            values = projections.get(position, [])
            index = max(0, demand[position] - drafted_counts[position])
            next_value = values[index] if index < len(values) else float("-inf")
            candidates.append((next_value, position))
        return max(candidates)[1] if candidates else None

    for eligible in flexible:
        if position := best_position(eligible):
            demand[position] += 1

    modeled_bench = round(bench_slots * config.strategy.replacement_bench_fraction)
    bench_positions = [position for position in projections if demand[position] > 0]
    starter_total = sum(demand[position] for position in bench_positions)
    if modeled_bench and starter_total:
        exact = {
            position: modeled_bench * demand[position] / starter_total
            for position in bench_positions
        }
        allocated = {position: int(value) for position, value in exact.items()}
        remaining = modeled_bench - sum(allocated.values())
        for position in sorted(
            bench_positions,
            key=lambda item: (exact[item] - allocated[item], demand[item]),
            reverse=True,
        )[:remaining]:
            allocated[position] += 1
        demand.update(allocated)

    levels: dict[str, float] = {}
    for position, values in projections.items():
        if not values:
            levels[position] = 0.0
            continue
        remaining_demand = max(0, demand[position] - drafted_counts[position])
        replacement_index = min(len(values) - 1, remaining_demand)
        levels[position] = values[replacement_index]
    return ReplacementProfile(dict(demand), levels)


def _starting_capacity(config: LeagueConfig) -> Counter[str]:
    capacity: Counter[str] = Counter()
    for slot in config.roster.slots:
        if slot.starter and slot.draftable:
            for position in slot.eligible_positions:
                capacity[position] += slot.count
    return capacity


def _needed_positions(state: DraftState) -> set[str]:
    needs = set(state.team_needs[state.config.draft.our_team_id])
    return {
        position
        for slot in state.config.roster.slots
        if slot.code in needs
        for position in slot.eligible_positions
    }


def select_advisor_candidates(
    state: DraftState,
    evaluated: Sequence[EvaluatedPlayer],
    *,
    limit: int,
) -> list[EvaluatedPlayer]:
    """Build a diverse, roster-aware allowlist from deterministic evaluations."""
    identity_counts = Counter(
        (normalize_name(item.player.name), item.player.primary_position)
        for item in evaluated
    )
    eligible = [
        item for item in evaluated
        if identity_counts[(normalize_name(item.player.name), item.player.primary_position)] == 1
    ]
    our_team = state.config.draft.our_team_id
    roster_counts = Counter(
        pick.player.primary_position for pick in state.picks if pick.team_id == our_team
    )
    capacity = _starting_capacity(state.config)
    needed = _needed_positions(state)
    current_round = (
        state.current_pick.round_number
        if getattr(state, "current_pick", None) is not None
        else 1
    )
    position_caps = {
        position: (
            0
            if (
                (maximum := state.config.strategy.max_roster_counts.get(position))
                is not None
                and roster_counts[position] >= maximum
            )
            else 1
            if (
                (target := state.config.strategy.position_target_rounds.get(position))
                is not None
                and current_round < target
            )
            else 3 if position in needed
            else 2 if roster_counts[position] > capacity[position]
            else 3 if roster_counts[position] == capacity[position]
            else 4
        )
        for position in {item.player.primary_position for item in eligible}
    }
    selected: list[EvaluatedPlayer] = []
    selected_ids: set[str] = set()
    selected_positions: Counter[str] = Counter()

    def add(item: EvaluatedPlayer) -> None:
        if item.player.id in selected_ids or len(selected) >= limit:
            return
        selected.append(item)
        selected_ids.add(item.player.id)
        selected_positions[item.player.primary_position] += 1

    for position in sorted(needed):
        for item in (candidate for candidate in eligible if candidate.player.primary_position == position):
            if selected_positions[position] >= min(3, position_caps.get(position, 0)):
                break
            add(item)
    for item in eligible:
        position = item.player.primary_position
        if selected_positions[position] < position_caps.get(position, 0):
            add(item)
    return sorted(selected, key=lambda item: item.quantitative_score, reverse=True)


def calculate_preference_adjustment(
    config: LeagueConfig,
    player: Player,
    metrics: PlayerMetrics,
    *,
    season: int | None,
    draft_progress: float,
    current_round: int | None = None,
) -> float:
    market_rank = metrics.ecr or metrics.adp or 250.0
    rookie_quality = max(
        player.upside or 0.0,
        max(0.0, 1.0 - market_rank / 250.0),
    )
    rookie_bonus = (
        config.strategy.rookie_late_round_bonus
        * draft_progress * draft_progress * rookie_quality
        if player.draft_year == season else 0.0
    )
    team_bonus = config.strategy.preferred_nfl_team_bonuses.get(
        (player.nfl_team or "").upper(), 0.0
    )
    target_round = config.strategy.position_target_rounds.get(player.primary_position)
    timing_penalty = (
        config.strategy.early_position_round_penalty
        * max(0, target_round - current_round)
        if target_round is not None and current_round is not None
        else 0.0
    )
    return rookie_bonus + team_bonus - timing_penalty


def calculate_tiers(
    values_by_position: dict[str, list[tuple[str, float]]]
) -> tuple[dict[str, int], dict[str, float]]:
    tiers: dict[str, int] = {}
    cliffs: dict[str, float] = {}
    for values in values_by_position.values():
        ordered = sorted(values, key=lambda item: item[1], reverse=True)
        tier = 1
        for index, (player_id, value) in enumerate(ordered):
            next_value = ordered[index + 1][1] if index + 1 < len(ordered) else value
            drop = max(0.0, value - next_value)
            tiers[player_id] = tier
            cliffs[player_id] = round(drop, 2)
            if drop >= max(6.0, abs(value) * 0.08):
                tier += 1
    return tiers, cliffs


def _max_position_counts(config: LeagueConfig) -> dict[str, int]:
    result: Counter[str] = Counter()
    for slot in config.roster.slots:
        if slot.draftable:
            for position in slot.eligible_positions:
                result[position] += slot.count
    return dict(result)


def simulate_survival(
    state: DraftState,
    candidates: Sequence[tuple[Player, PlayerMetrics]],
    *,
    simulations: int,
) -> dict[str, float | None]:
    if state.next_user_pick is None or not state.teams_before_user_pick:
        return {player.id: None for player, _ in candidates}
    pool = sorted(candidates, key=lambda item: item[1].adp or item[1].ecr or 999)[:140]
    tracked = {player.id for player, _ in pool[:40]}
    survived: Counter[str] = Counter()
    max_counts = _max_position_counts(state.config)
    initial_counts: dict[str, Counter[str]] = {
        team.id: Counter(
            pick.player.primary_position for pick in state.picks if pick.team_id == team.id
        )
        for team in state.config.teams
    }
    seed_material = f"{state.draft_id}:{len(state.picks)}:{state.next_user_pick}".encode()
    rng = random.Random(int(hashlib.sha256(seed_material).hexdigest()[:16], 16))
    for _ in range(simulations):
        available = {player.id: (player, metrics) for player, metrics in pool}
        market_draw = {
            player.id: rng.gauss(
                metrics.adp or metrics.ecr or 250.0,
                max(4.0, metrics.rank_stddev or 10.0),
            )
            for player, metrics in pool
        }
        market_order = sorted(market_draw, key=market_draw.__getitem__)
        counts = {team: Counter(values) for team, values in initial_counts.items()}
        last_position: str | None = None
        for team_id in state.teams_before_user_pick:
            needs = state.team_needs.get(team_id, [])
            selected: tuple[float, str, str] | None = None
            for player_id in market_order:
                record = available.get(player_id)
                if record is None:
                    continue
                player, _metrics = record
                position = player.primary_position
                if counts[team_id][position] >= max_counts.get(position, 99):
                    continue
                # No unseen player can gain more than 7.5 points from need + run.
                if selected is not None and market_draw[player_id] > selected[0] + 7.5:
                    break
                draw = market_draw[player_id]
                if position in needs:
                    draw -= 6.0
                if position == last_position:
                    draw -= 1.5
                choice = (draw, player_id, position)
                if selected is None or choice < selected:
                    selected = choice
            if selected is None:
                break
            _, selected_id, last_position = selected
            counts[team_id][last_position] += 1
            available.pop(selected_id, None)
        for player_id in tracked & available.keys():
            survived[player_id] += 1
    return {
        player.id: (survived[player.id] / simulations if player.id in tracked else None)
        for player, _ in candidates
    }


class BaselinePlayerEvaluator:
    """Transparent league-adjusted deterministic evaluator with Monte Carlo survival."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        simulations: int = 2000,
    ):
        self.session_factory = session_factory
        self.simulations = simulations

    def _snapshot_run(self, session: Session, state: DraftState, dataset: str) -> int | None:
        key = f"fantasypros:{dataset}"
        if key in state.data_snapshot:
            return int(state.data_snapshot[key])
        return None

    def _metrics(self, state: DraftState, players: Sequence[Player]) -> dict[str, PlayerMetrics]:
        fallback = {
            player.id: PlayerMetrics(
                float(player.projected_points or 0),
                float(player.overall_rank) if player.overall_rank else None,
                player.adp, player.tier, None,
            ) for player in players
        }
        if self.session_factory is None:
            return fallback
        with self.session_factory() as session:
            rank_run = self._snapshot_run(session, state, "rankings")
            adp_run = self._snapshot_run(session, state, "adp")
            projection_run = self._snapshot_run(session, state, "projections")
            rankings = {row.player_id: row for row in session.scalars(
                select(PlayerRanking).where(PlayerRanking.import_run_id == rank_run)
            )} if rank_run else {}
            adps = {row.player_id: row for row in session.scalars(
                select(PlayerRanking).where(PlayerRanking.import_run_id == adp_run)
            )} if adp_run else {}
            projections = {row.player_id: row for row in session.scalars(
                select(PlayerProjection).where(PlayerProjection.import_run_id == projection_run)
            )} if projection_run else {}
            result = {}
            for player in players:
                ranking = rankings.get(player.id)
                adp_row = adps.get(player.id)
                projection = projections.get(player.id)
                scored = score_projection(projection.stats, player.primary_position, state.config) if projection else None
                result[player.id] = PlayerMetrics(
                    scored.points if scored else fallback[player.id].projected_points,
                    ranking.overall_rank if ranking else fallback[player.id].ecr,
                    (adp_row.adp if adp_row else None) or (ranking.adp if ranking else None) or fallback[player.id].adp,
                    ranking.provider_tier if ranking else fallback[player.id].provider_tier,
                    ranking.rank_stddev if ranking else None,
                    scored.approximations if scored else (),
                )
            return result

    def evaluate(self, state: DraftState, players: Sequence[Player]) -> list[EvaluatedPlayer]:
        metrics = self._metrics(state, players)
        drafted_counts = Counter(pick.player.primary_position for pick in state.picks)
        projections: dict[str, list[float]] = defaultdict(list)
        for player in players:
            projections[player.primary_position].append(metrics[player.id].projected_points)
        for values in projections.values():
            values.sort(reverse=True)
        replacement = calculate_replacement_profile(
            state.config, projections, drafted_counts
        )
        vors = {
            player.id: metrics[player.id].projected_points - replacement.levels.get(player.primary_position, 0)
            for player in players
        }
        by_position: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for player in players:
            by_position[player.primary_position].append((player.id, vors[player.id]))
        our_tiers, cliffs = calculate_tiers(by_position)
        survival = simulate_survival(
            state, [(player, metrics[player.id]) for player in players],
            simulations=self.simulations,
        )
        our_team = state.config.draft.our_team_id
        needed_positions = _needed_positions(state)
        our_counts = Counter(
            pick.player.primary_position for pick in state.picks if pick.team_id == our_team
        )
        starting_capacity = _starting_capacity(state.config)
        required_remaining = len(state.team_needs[our_team])
        picks_remaining = max(0, state.config.draft.rounds - our_counts.total())
        required_slack = max(0, picks_remaining - required_remaining)
        progress = state.current_pick.round_number / state.config.draft.rounds if state.current_pick else 1
        results = []
        for player in players:
            metric = metrics[player.id]
            probability = survival[player.id]
            scarcity = cliffs[player.id]
            cost_waiting = scarcity * (1 - probability) if probability is not None else 0.0
            is_needed = player.primary_position in needed_positions
            need_multiplier = 2.0 if required_slack <= 1 and required_remaining else 1.0
            need_bonus = (
                state.config.strategy.required_starter_bonus
                * progress * progress
                * need_multiplier
                if is_needed else 0.0
            )
            surplus_before = max(
                0, our_counts[player.primary_position] - starting_capacity[player.primary_position]
            )
            surplus_penalty = (
                state.config.strategy.surplus_position_penalty * surplus_before
                if not is_needed else 0.0
            )
            roster_fit = need_bonus - surplus_penalty
            if is_needed or our_counts[player.primary_position] < starting_capacity[player.primary_position]:
                vor_utility = 1.0
            elif surplus_before == 0:
                vor_utility = 0.45
            else:
                vor_utility = 0.15
            upside = (player.upside or 0) * (4 + 12 * progress)
            risk = (player.injury_risk or 0) * (12 - 6 * progress)
            market_value = max(-15, min(15, (state.current_pick.overall if state.current_pick else 1) - (metric.ecr or metric.adp or 999)))
            preference = calculate_preference_adjustment(
                state.config,
                player,
                metric,
                season=state.season,
                draft_progress=progress,
                current_round=state.current_pick.round_number if state.current_pick else None,
            )
            score = (
                vors[player.id] * 1.6 * vor_utility
                + scarcity * .7
                + cost_waiting * 1.8
                + roster_fit
                + upside
                - risk
                + market_value
                + preference
            )
            results.append(EvaluatedPlayer(
                player, round(metric.projected_points, 2), round(vors[player.id], 2),
                round(replacement.levels.get(player.primary_position, 0), 2),
                metric.ecr, metric.adp, metric.provider_tier, our_tiers[player.id],
                cliffs[player.id], scarcity, probability, round(cost_waiting, 2),
                round(roster_fit, 2), round(upside, 2), round(-risk, 2),
                round(preference, 2), round(score, 2),
                self.simulations, metric.approximations,
            ))
        return sorted(results, key=lambda item: item.quantitative_score, reverse=True)


@dataclass(frozen=True)
class Recommendation:
    category: str
    player: Player
    explanation: str
    confidence: int


@dataclass(frozen=True)
class RecommendationSet:
    recommendations: list[Recommendation]
    preferred: Recommendation | None
    next_pick_strategy: str
    source: str
    overall_confidence: int | None = None
    reason: str | None = None
    latency_ms: int | None = None
    model_used: str | None = None
    reasoning_effort: str | None = None
    is_fallback: bool = False
    fallback_reason: str | None = None
    configured_model: str | None = None
    configured_timeout_seconds: float | None = None
    response_status: str | None = None


class StrategicAdvisor(Protocol):
    def recommend(self, state: DraftState, evaluated: Sequence[EvaluatedPlayer]) -> RecommendationSet: ...


class ResilientStrategicAdvisor:
    def __init__(self, primary: StrategicAdvisor, fallback: StrategicAdvisor):
        self.primary, self.fallback = primary, fallback

    def recommend(self, state: DraftState, evaluated: Sequence[EvaluatedPlayer]) -> RecommendationSet:
        try:
            return self.primary.recommend(state, evaluated)
        except Exception as exc:
            if type(exc).__name__ == "AdvisorUnavailable":
                logger.info("AI unavailable; using deterministic fallback: %s", exc)
                reason = str(exc)
            else:
                logger.warning("Strategic advisor failed; using deterministic fallback", exc_info=True)
                reason = f"AI request failed ({type(exc).__name__})."
            return replace(
                self.fallback.recommend(state, evaluated),
                is_fallback=True,
                fallback_reason=reason,
                configured_model=getattr(self.primary, "model", None),
                configured_timeout_seconds=getattr(
                    self.primary, "timeout_seconds", None
                ),
                reasoning_effort=getattr(self.primary, "reasoning_effort", None),
            )


class OfflineStrategicAdvisor:
    def recommend(self, state: DraftState, evaluated: Sequence[EvaluatedPlayer]) -> RecommendationSet:
        pool = select_advisor_candidates(state, evaluated, limit=30)
        if not pool:
            return RecommendationSet(
                [],
                None,
                "No available players.",
                "AI unavailable — quantitative fallback",
                is_fallback=True,
                fallback_reason="No available candidates were provided to the advisor.",
            )
        selected: set[str] = set()

        def choose(category: str, key, reason) -> Recommendation | None:
            choices = [item for item in pool if item.player.id not in selected]
            if not choices:
                return None
            item = max(choices, key=key)
            selected.add(item.player.id)
            confidence = min(92, max(55, int(68 + item.quantitative_score / 25)))
            return Recommendation(category, item.player, reason(item), confidence)

        choices = [
            choose("BEST", lambda item: item.quantitative_score, lambda item: f"{item.vor:+.1f} VOR with {item.cost_of_waiting:.1f} cost of waiting."),
            choose("SAFE", lambda item: item.quantitative_score + item.risk_adjustment, lambda item: f"Projects for {item.projected_points:.1f} league points with roster-adjusted baseline value."),
            choose("UPSIDE", lambda item: item.quantitative_score + item.upside_adjustment + item.tier_cliff, lambda item: f"Upside profile plus a {item.tier_cliff:.1f}-point tier cliff."),
            choose("VALUE", lambda item: item.quantitative_score + max(0, (state.current_pick.overall if state.current_pick else 1) - (item.ecr or 999)), lambda item: f"ECR {item.ecr or '—'} versus current pick, adjusted for roster utility."),
            choose("STRATEGIC", lambda item: item.quantitative_score + item.cost_of_waiting, lambda item: f"{((item.survival_probability or 0) * 100):.0f}% likely to survive; waiting costs {item.cost_of_waiting:.1f}."),
        ]
        recommendations = [item for item in choices if item]
        positions = ", ".join(dict.fromkeys(item.player.primary_position for item in recommendations[1:4]))
        return RecommendationSet(
            recommendations, recommendations[0] if recommendations else None,
            f"Monitor {positions or 'the next value tier'} and recalculate after every pick.",
            "AI unavailable — quantitative fallback",
            is_fallback=True,
        )
