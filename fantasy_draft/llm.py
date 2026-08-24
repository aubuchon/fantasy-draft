from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from fantasy_draft.evaluation import (
    EvaluatedPlayer,
    Recommendation,
    RecommendationSet,
)
from fantasy_draft.models import RecommendationHistory
from fantasy_draft.services import DraftState


class AdvisorUnavailable(RuntimeError):
    pass


class AdvisorValidationError(ValueError):
    pass


class LLMRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player_id: str
    label: Literal["BEST", "SAFE", "UPSIDE", "VALUE", "STRATEGIC"]
    note: str = Field(min_length=1, max_length=240)
    confidence: float = Field(ge=0, le=1)


class LLMAdvisorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendations: list[LLMRecommendation] = Field(min_length=5, max_length=5)
    preferred_player_id: str
    overall_confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=320)
    next_pick_strategy: str = Field(min_length=1, max_length=320)

    @model_validator(mode="after")
    def validate_categories(self) -> "LLMAdvisorOutput":
        expected = {"BEST", "SAFE", "UPSIDE", "VALUE", "STRATEGIC"}
        if {item.label for item in self.recommendations} != expected:
            raise ValueError("recommendations must contain each required label exactly once")
        if len({item.player_id for item in self.recommendations}) != 5:
            raise ValueError("recommendation player IDs must be unique")
        if self.preferred_player_id not in {item.player_id for item in self.recommendations}:
            raise ValueError("preferred player must be one of the recommendations")
        return self


class ResponsesClient(Protocol):
    responses: Any


class OpenAIStrategicAdvisor:
    """Bounded, validated strategic reasoning over deterministic candidates only."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        api_key: str | None,
        model: str = "gpt-5.6",
        timeout_seconds: float = 5.0,
        reasoning_effort: str = "low",
        prefetch_picks: int = 3,
        client: ResponsesClient | None = None,
    ):
        self.session_factory = session_factory
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.reasoning_effort = reasoning_effort
        self.prefetch_picks = prefetch_picks
        self._client = client

    def _candidate_packet(
        self, state: DraftState, candidates: Sequence[EvaluatedPlayer]
    ) -> dict[str, Any]:
        team_names = {team.id: team.name for team in state.config.teams}
        rosters = {
            team.id: [
                {"player_id": pick.player.id, "position": pick.player.primary_position}
                for pick in state.picks if pick.team_id == team.id
            ]
            for team in state.config.teams
        }
        return {
            "league": {
                "teams": state.config.league.team_count,
                "scoring_type": state.config.league.scoring_type,
                "roster_slots": [slot.model_dump() for slot in state.config.roster.slots],
            },
            "draft": {
                "round": state.current_pick.round_number if state.current_pick else None,
                "overall_pick": state.current_pick.overall if state.current_pick else None,
                "our_next_pick": state.next_user_pick,
                "picks_until_next": state.picks_until_user_pick,
            },
            "our_roster": rosters[state.config.draft.our_team_id],
            "our_needs": state.team_needs[state.config.draft.our_team_id],
            "teams_before_next_pick": [
                {
                    "team_id": team_id,
                    "name": team_names[team_id],
                    "roster": rosters[team_id],
                    "needs": state.team_needs[team_id],
                }
                for team_id in dict.fromkeys(state.teams_before_user_pick)
            ],
            "allowed_candidates": [
                {
                    "player_id": item.player.id,
                    "name": item.player.name,
                    "position": item.player.primary_position,
                    "nfl_team": item.player.nfl_team,
                    "league_projected_points": item.projected_points,
                    "vor": item.vor,
                    "ecr": item.ecr,
                    "adp": item.adp,
                    "provider_tier": item.provider_tier,
                    "our_tier": item.our_tier,
                    "tier_cliff": item.tier_cliff,
                    "scarcity": item.scarcity,
                    "survival_probability": item.survival_probability,
                    "cost_of_waiting": item.cost_of_waiting,
                    "roster_fit": item.roster_fit,
                    "upside_adjustment": item.upside_adjustment,
                    "risk_adjustment": item.risk_adjustment,
                    "quantitative_score": item.quantitative_score,
                }
                for item in candidates
            ],
        }

    def _fingerprint(self, packet: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _validate_and_convert(
        self,
        output: LLMAdvisorOutput,
        candidates: Sequence[EvaluatedPlayer],
        *,
        latency_ms: int | None,
    ) -> RecommendationSet:
        by_id = {item.player.id: item.player for item in candidates}
        returned = {item.player_id for item in output.recommendations}
        invalid = sorted(returned - by_id.keys())
        if invalid:
            raise AdvisorValidationError("advisor returned a player outside the allowed candidate set")
        recommendations = [
            Recommendation(
                item.label, by_id[item.player_id], item.note, round(item.confidence * 100)
            )
            for item in output.recommendations
        ]
        preferred = next(
            item for item in recommendations if item.player.id == output.preferred_player_id
        )
        return RecommendationSet(
            recommendations=recommendations,
            preferred=preferred,
            next_pick_strategy=output.next_pick_strategy,
            source=f"OpenAI {self.model}",
            overall_confidence=round(output.overall_confidence * 100),
            reason=output.reason,
            latency_ms=latency_ms,
        )

    def _cached(
        self, state: DraftState, fingerprint: str, candidates: Sequence[EvaluatedPlayer]
    ) -> RecommendationSet | None:
        with self.session_factory() as session:
            history = session.scalar(
                select(RecommendationHistory)
                .where(
                    RecommendationHistory.draft_id == state.draft_id,
                    RecommendationHistory.request_fingerprint == fingerprint,
                    RecommendationHistory.source == "openai",
                )
                .order_by(RecommendationHistory.id.desc())
                .limit(1)
            )
            if history is None:
                return None
            output = LLMAdvisorOutput.model_validate(history.response)
            return self._validate_and_convert(
                output, candidates, latency_ms=history.latency_ms
            )

    def recommend(
        self,
        state: DraftState,
        evaluated: Sequence[EvaluatedPlayer],
        *,
        force: bool = False,
        persist: bool = True,
    ) -> RecommendationSet:
        available_ids = {player.id for player in state.available_players}
        candidates = [item for item in evaluated if item.player.id in available_ids][:20]
        if len(candidates) < 5:
            raise AdvisorUnavailable("at least five candidates are required")
        packet = self._candidate_packet(state, candidates)
        fingerprint = self._fingerprint(packet)
        if cached := self._cached(state, fingerprint, candidates):
            return cached
        our_turn = state.current_team_id == state.config.draft.our_team_id
        if not force and not our_turn and (
            state.picks_until_user_pick is None
            or state.picks_until_user_pick > self.prefetch_picks
        ):
            raise AdvisorUnavailable("AI prefetch begins as our pick approaches")
        if not self.api_key:
            raise AdvisorUnavailable("OPENAI_API_KEY is not configured")
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.api_key,
                timeout=self.timeout_seconds,
                max_retries=0,
            )
        started = time.perf_counter()
        response = self._client.responses.parse(
            model=self.model,
            reasoning={"effort": self.reasoning_effort},
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a live fantasy draft strategist. Use only allowed candidate IDs. "
                        "Deterministic state is authoritative. Be terse, distinguish value now from "
                        "probability of surviving to the next pick, and never propose a roster mutation."
                    ),
                },
                {"role": "user", "content": json.dumps(packet, separators=(",", ":"))},
            ],
            text_format=LLMAdvisorOutput,
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        output = response.output_parsed
        if output is None:
            raise AdvisorValidationError("advisor returned no structured output")
        if not isinstance(output, LLMAdvisorOutput):
            output = LLMAdvisorOutput.model_validate(output)
        result = self._validate_and_convert(output, candidates, latency_ms=latency_ms)
        if persist:
            with self.session_factory.begin() as session:
                session.add(RecommendationHistory(
                    draft_id=state.draft_id,
                    overall_pick=state.current_pick.overall if state.current_pick else 0,
                    source="openai",
                    model=self.model,
                    request_fingerprint=fingerprint,
                    candidates=[item.player.id for item in candidates],
                    response=output.model_dump(mode="json"),
                    latency_ms=latency_ms,
                ))
        return result
