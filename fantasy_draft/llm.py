from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from fantasy_draft.evaluation import (
    EvaluatedPlayer,
    Recommendation,
    RecommendationSet,
)
from fantasy_draft.models import RecommendationHistory
from fantasy_draft.services import DraftState


logger = logging.getLogger(__name__)


class AdvisorUnavailable(RuntimeError):
    pass


class AdvisorValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "validation",
        model_used: str | None = None,
        response_status: str | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.model_used = model_used
        self.response_status = response_status


@dataclass(frozen=True)
class AdvisorDiagnostic:
    success: bool
    configured_model: str
    model_used: str | None
    reasoning_effort: str
    timeout_seconds: float
    max_retries: int
    latency_ms: int
    structured_output_valid: bool
    failure_category: str | None = None
    exception_type: str | None = None
    response_status: str | None = None


def classify_openai_failure(exc: Exception) -> str:
    """Return a credential-safe, transport-specific diagnostic category."""
    import httpx
    from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
    from pydantic import ValidationError

    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    timeout_types = (
        (httpx.ConnectTimeout, "timeout.connect"),
        (httpx.ReadTimeout, "timeout.read"),
        (httpx.WriteTimeout, "timeout.write"),
        (httpx.PoolTimeout, "timeout.pool"),
    )
    for error in chain:
        for timeout_type, category in timeout_types:
            if isinstance(error, timeout_type):
                return category
    if isinstance(exc, APITimeoutError):
        return "timeout.unknown"
    if isinstance(exc, RateLimitError):
        return "api.rate_limit"
    if isinstance(exc, APIStatusError):
        return f"api.http_{exc.status_code}"
    if isinstance(exc, APIConnectionError):
        return "connection.transport"
    if isinstance(exc, AdvisorValidationError):
        return f"structured_output.{exc.category}"
    if isinstance(exc, ValidationError):
        return "structured_output.schema_validation"
    if isinstance(exc, AdvisorUnavailable):
        return "configuration.unavailable"
    return f"unexpected.{type(exc).__name__}"


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
        model: str = "gpt-5.6-terra",
        timeout_seconds: float = 25.0,
        reasoning_effort: str = "low",
        client: ResponsesClient | None = None,
    ):
        self.session_factory = session_factory
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.reasoning_effort = reasoning_effort
        self.max_retries = 0
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
            json.dumps(
                {
                    "packet": packet,
                    "model": self.model,
                    "reasoning_effort": self.reasoning_effort,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def _response_format(
        self, candidates: Sequence[EvaluatedPlayer]
    ) -> type[LLMAdvisorOutput]:
        """Build a strict schema whose player IDs are limited to this candidate set."""
        candidate_ids = tuple(item.player.id for item in candidates)
        allowed_player_id = Literal.__getitem__(candidate_ids)
        suffix = hashlib.sha256("\n".join(candidate_ids).encode()).hexdigest()[:12]
        recommendation_model = create_model(
            f"DraftRecommendation_{suffix}",
            __base__=LLMRecommendation,
            player_id=(allowed_player_id, ...),
        )
        return create_model(
            f"DraftAdvisorOutput_{suffix}",
            __base__=LLMAdvisorOutput,
            recommendations=(
                list[recommendation_model],
                Field(min_length=5, max_length=5),
            ),
            preferred_player_id=(allowed_player_id, ...),
        )

    def _missing_output_error(self, response: Any) -> AdvisorValidationError:
        model_used = str(getattr(response, "model", None) or self.model)
        status = str(getattr(response, "status", None) or "unknown")
        refusal = any(
            getattr(content, "type", None) == "refusal"
            for output in (getattr(response, "output", None) or [])
            if getattr(output, "type", None) == "message"
            for content in (getattr(output, "content", None) or [])
        )
        if refusal:
            category = "refusal"
        elif status == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = str(getattr(details, "reason", None) or "unknown")
            safe_reason = re.sub(r"[^a-z0-9_]+", "_", reason.lower()).strip("_")
            category = f"incomplete_{safe_reason or 'unknown'}"
        elif status == "failed":
            category = "response_failed"
        else:
            category = "missing_parsed_output"
        return AdvisorValidationError(
            "advisor returned no structured output",
            category=category,
            model_used=model_used,
            response_status=status,
        )

    def _validate_and_convert(
        self,
        output: LLMAdvisorOutput,
        candidates: Sequence[EvaluatedPlayer],
        *,
        latency_ms: int | None,
        model_used: str | None = None,
        reasoning_effort: str | None = None,
        response_status: str | None = None,
    ) -> RecommendationSet:
        by_id = {item.player.id: item.player for item in candidates}
        returned = {item.player_id for item in output.recommendations}
        invalid = sorted(returned - by_id.keys())
        if invalid:
            raise AdvisorValidationError(
                "advisor returned a player outside the allowed candidate set",
                category="candidate_not_allowed",
                model_used=model_used or self.model,
                response_status=response_status,
            )
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
            source=f"OpenAI {model_used or self.model}",
            overall_confidence=round(output.overall_confidence * 100),
            reason=output.reason,
            latency_ms=latency_ms,
            model_used=model_used or self.model,
            reasoning_effort=reasoning_effort or self.reasoning_effort,
            configured_model=self.model,
            configured_timeout_seconds=self.timeout_seconds,
            response_status=response_status,
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
                output,
                candidates,
                latency_ms=history.latency_ms,
                model_used=history.model,
                reasoning_effort=self.reasoning_effort,
                response_status="completed",
            )

    def _cached_failure(self, state: DraftState, fingerprint: str) -> str | None:
        with self.session_factory() as session:
            history = session.scalar(
                select(RecommendationHistory)
                .where(
                    RecommendationHistory.draft_id == state.draft_id,
                    RecommendationHistory.request_fingerprint == fingerprint,
                    RecommendationHistory.source == "openai_error",
                )
                .order_by(RecommendationHistory.id.desc())
                .limit(1)
            )
            if history is None:
                return None
            return str(history.response.get("failure_category") or "unknown")

    def _persist_failure(
        self,
        state: DraftState,
        fingerprint: str,
        candidates: Sequence[EvaluatedPlayer],
        exc: Exception,
        latency_ms: int,
    ) -> None:
        try:
            with self.session_factory.begin() as session:
                session.add(RecommendationHistory(
                    draft_id=state.draft_id,
                    overall_pick=state.current_pick.overall if state.current_pick else 0,
                    source="openai_error",
                    model=getattr(exc, "model_used", None) or self.model,
                    request_fingerprint=fingerprint,
                    candidates=[item.player.id for item in candidates],
                    response={
                        "failure_category": classify_openai_failure(exc),
                        "exception_type": type(exc).__name__,
                        "response_status": getattr(exc, "response_status", None),
                    },
                    latency_ms=latency_ms,
                ))
        except Exception:
            logger.warning(
                "Could not persist AI failure marker; deterministic fallback remains active",
                exc_info=True,
            )

    def recommend(
        self,
        state: DraftState,
        evaluated: Sequence[EvaluatedPlayer],
        *,
        force: bool = False,
        persist: bool = True,
        use_cache: bool = True,
    ) -> RecommendationSet:
        available_ids = {player.id for player in state.available_players}
        candidates = [item for item in evaluated if item.player.id in available_ids][:20]
        if len(candidates) < 5:
            raise AdvisorUnavailable("at least five candidates are required")
        packet = self._candidate_packet(state, candidates)
        fingerprint = self._fingerprint(packet)
        if use_cache:
            if cached := self._cached(state, fingerprint, candidates):
                return cached
            if failure_category := self._cached_failure(state, fingerprint):
                raise AdvisorUnavailable(
                    f"Previous {self.model} attempt failed ({failure_category}). "
                    "Use Try AI again to make another request."
                )
        our_turn = state.current_team_id == state.config.draft.our_team_id
        if not force and not our_turn:
            raise AdvisorUnavailable(
                "AI automatically runs only when our team is on the clock."
            )
        if not self.api_key:
            raise AdvisorUnavailable("OPENAI_API_KEY is not configured")
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.api_key,
                timeout=self.timeout_seconds,
                max_retries=self.max_retries,
            )
        started = time.perf_counter()
        try:
            response_format = self._response_format(candidates)
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
                text_format=response_format,
            )
            latency_ms = round((time.perf_counter() - started) * 1000)
            output = response.output_parsed
            if output is None:
                raise self._missing_output_error(response)
            if not isinstance(output, LLMAdvisorOutput):
                output = LLMAdvisorOutput.model_validate(output)
            model_used = str(getattr(response, "model", None) or self.model)
            response_reasoning = getattr(response, "reasoning", None)
            reasoning_effort = str(
                getattr(response_reasoning, "effort", None) or self.reasoning_effort
            )
            result = self._validate_and_convert(
                output,
                candidates,
                latency_ms=latency_ms,
                model_used=model_used,
                reasoning_effort=reasoning_effort,
                response_status=str(getattr(response, "status", None) or "completed"),
            )
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000)
            if persist:
                self._persist_failure(
                    state, fingerprint, candidates, exc, latency_ms
                )
            raise
        if persist:
            with self.session_factory.begin() as session:
                session.add(RecommendationHistory(
                    draft_id=state.draft_id,
                    overall_pick=state.current_pick.overall if state.current_pick else 0,
                    source="openai",
                    model=model_used,
                    request_fingerprint=fingerprint,
                    candidates=[item.player.id for item in candidates],
                    response=output.model_dump(mode="json"),
                    latency_ms=latency_ms,
                ))
        return result

    def diagnose(
        self, state: DraftState, evaluated: Sequence[EvaluatedPlayer]
    ) -> AdvisorDiagnostic:
        """Make an uncached diagnostic request and return credential-safe telemetry."""
        started = time.perf_counter()
        try:
            result = self.recommend(
                state,
                evaluated,
                force=True,
                persist=False,
                use_cache=False,
            )
        except Exception as exc:
            return AdvisorDiagnostic(
                success=False,
                configured_model=self.model,
                model_used=getattr(exc, "model_used", None),
                reasoning_effort=self.reasoning_effort,
                timeout_seconds=self.timeout_seconds,
                max_retries=self.max_retries,
                latency_ms=round((time.perf_counter() - started) * 1000),
                structured_output_valid=False,
                failure_category=classify_openai_failure(exc),
                exception_type=type(exc).__name__,
                response_status=getattr(exc, "response_status", None),
            )
        return AdvisorDiagnostic(
            success=True,
            configured_model=self.model,
            model_used=result.model_used or self.model,
            reasoning_effort=result.reasoning_effort or self.reasoning_effort,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            latency_ms=result.latency_ms or round((time.perf_counter() - started) * 1000),
            structured_output_valid=True,
            response_status=result.response_status or "completed",
        )
