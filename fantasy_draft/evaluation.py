from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Protocol, Sequence

from fantasy_draft.models import Player
from fantasy_draft.services import DraftState


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvaluatedPlayer:
    player: Player
    quantitative_score: float
    survival_probability: float | None


class PlayerEvaluationProvider(Protocol):
    def evaluate(
        self, state: DraftState, players: Sequence[Player]
    ) -> list[EvaluatedPlayer]: ...


class BaselinePlayerEvaluator:
    """Offline baseline; replaceable by projections/ADP/VOR provider implementations."""

    def evaluate(
        self, state: DraftState, players: Sequence[Player]
    ) -> list[EvaluatedPlayer]:
        current = state.current_pick.overall if state.current_pick else 1
        next_pick = state.next_user_pick
        total_picks = state.config.league.team_count * state.config.draft.rounds
        draft_progress = min(1, current / max(1, total_picks))
        our_needs = set(state.team_needs[state.config.draft.our_team_id])
        intervening_needs = [
            need
            for team_id in state.teams_before_user_pick
            for need in state.team_needs[team_id]
        ]
        results: list[EvaluatedPlayer] = []
        for player in players:
            rank = player.overall_rank or 999
            projection = player.projected_points or 0
            base = 1000 - (rank * 5) + projection
            if player.primary_position in our_needs:
                base += 12
            base += max(0, 7 - (player.tier or 7)) * 2
            base += min(12, intervening_needs.count(player.primary_position) * 1.5)
            early_profile = (player.floor or 0) * .03 + (player.role_certainty or 0) * 10
            late_profile = (player.ceiling or 0) * .02 + (player.upside or 0) * 20
            base += (1 - draft_progress) * early_profile + draft_progress * late_profile
            survival = None
            if next_pick is not None:
                expected_pick = player.adp or float(rank)
                survival = 1 / (1 + math.exp(-(expected_pick - next_pick) / 8))
                urgency = (1 - survival) * min(50, max(0, next_pick - current))
                base += urgency
            results.append(EvaluatedPlayer(player, round(base, 2), survival))
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


class StrategicAdvisor(Protocol):
    def recommend(
        self, state: DraftState, evaluated: Sequence[EvaluatedPlayer]
    ) -> RecommendationSet: ...


class ResilientStrategicAdvisor:
    """Keep the draft usable when an external/model advisor fails or times out."""

    def __init__(self, primary: StrategicAdvisor, fallback: StrategicAdvisor):
        self.primary = primary
        self.fallback = fallback

    def recommend(
        self, state: DraftState, evaluated: Sequence[EvaluatedPlayer]
    ) -> RecommendationSet:
        try:
            return self.primary.recommend(state, evaluated)
        except Exception:
            logger.warning("Strategic advisor failed; using offline fallback", exc_info=True)
            return self.fallback.recommend(state, evaluated)


class OfflineStrategicAdvisor:
    """Fast deterministic fallback and stable boundary for a future LLM advisor."""

    def recommend(
        self, state: DraftState, evaluated: Sequence[EvaluatedPlayer]
    ) -> RecommendationSet:
        pool = list(evaluated[:30])
        if not pool:
            return RecommendationSet([], None, "No available players in the current filter.", "offline")

        selected: set[str] = set()

        def choose(category: str, key, reason) -> Recommendation | None:
            candidates = [item for item in pool if item.player.id not in selected]
            if not candidates:
                return None
            item = max(candidates, key=key)
            selected.add(item.player.id)
            return Recommendation(
                category=category,
                player=item.player,
                explanation=reason(item),
                confidence=min(92, max(55, int(62 + item.quantitative_score / 100))),
            )

        choices = [
            choose(
                "BEST PICK",
                lambda item: item.quantitative_score,
                lambda item: "Best combined baseline value and next-pick urgency.",
            ),
            choose(
                "SAFEST",
                lambda item: (item.player.floor or 0) + 50 * (item.player.role_certainty or 0) - 30 * (item.player.injury_risk or 0),
                lambda item: "Strong floor and role-certainty profile in the available pool.",
            ),
            choose(
                "UPSIDE",
                lambda item: (item.player.ceiling or 0) + 50 * (item.player.upside or 0),
                lambda item: "Highest ceiling and upside combination among top candidates.",
            ),
            choose(
                "VALUE",
                lambda item: (state.current_pick.overall if state.current_pick else 1) - (item.player.overall_rank or 999),
                lambda item: f"Ranked #{item.player.overall_rank or '—'} and still available at this pick.",
            ),
            choose(
                "STRATEGIC",
                lambda item: (1 - (item.survival_probability if item.survival_probability is not None else .5)) * 100 - (item.player.tier or 9),
                lambda item: "Low estimated chance to survive until our next selection.",
            ),
        ]
        recommendations = [choice for choice in choices if choice is not None]
        preferred = recommendations[0] if recommendations else None
        positions = ", ".join(dict.fromkeys(
            recommendation.player.primary_position for recommendation in recommendations[1:4]
        ))
        strategy = (
            f"Monitor {positions or 'the next value tier'} before our next pick; "
            "re-evaluate after every selection."
        )
        return RecommendationSet(recommendations, preferred, strategy, "offline baseline")
