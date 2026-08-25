from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError

from fantasy_draft.config import configure_draft_session
from fantasy_draft.evaluation import (
    BaselinePlayerEvaluator,
    OfflineStrategicAdvisor,
    ResilientStrategicAdvisor,
)
from fantasy_draft.llm import (
    AdvisorUnavailable,
    AdvisorValidationError,
    LLMAdvisorOutput,
    OpenAIStrategicAdvisor,
    classify_openai_failure,
)


def valid_output(ids):
    return LLMAdvisorOutput.model_validate({
        "recommendations": [
            {"player_id": player_id, "label": label, "note": f"Short {label} case.", "confidence": .8}
            for player_id, label in zip(ids, ["BEST", "SAFE", "UPSIDE", "VALUE", "STRATEGIC"])
        ],
        "preferred_player_id": ids[0],
        "overall_confidence": .84,
        "reason": "Best combination of value and urgency.",
        "next_pick_strategy": "Watch the next RB and WR tiers.",
    })


class FakeResponses:
    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.calls = 0

    def parse(self, **kwargs):
        self.calls += 1
        assert issubclass(kwargs["text_format"], LLMAdvisorOutput)
        if self.error:
            raise self.error
        return SimpleNamespace(
            output_parsed=self.output,
            model="gpt-5.6-sol",
            reasoning=SimpleNamespace(effort="low"),
            status="completed",
            output=[],
        )


def setup_state(service):
    draft_service, draft_id, session_factory = service
    state = draft_service.get_state(draft_id)
    evaluated = BaselinePlayerEvaluator(simulations=100).evaluate(state, state.available_players)
    return state, evaluated, session_factory


def setup_user_turn(service):
    draft_service, draft_id, session_factory = service
    state = draft_service.get_state(draft_id)
    config = configure_draft_session(
        state.config,
        team_count=state.config.league.team_count,
        our_draft_slot=2,
    )
    draft_id = draft_service.create_draft(config, name="Advisor unit test")
    state = draft_service.get_state(draft_id)
    while state.current_team_id != state.config.draft.our_team_id:
        draft_service.make_pick(draft_id, state.available_players[0].id)
        state = draft_service.get_state(draft_id)
    evaluated = BaselinePlayerEvaluator(simulations=100).evaluate(
        state, state.available_players
    )
    return state, evaluated, session_factory


def test_valid_structured_result_is_persisted_and_cached(service):
    state, evaluated, session_factory = setup_state(service)
    responses = FakeResponses(valid_output([item.player.id for item in evaluated[:5]]))
    advisor = OpenAIStrategicAdvisor(
        session_factory, api_key="test-value", client=SimpleNamespace(responses=responses)
    )
    first = advisor.recommend(state, evaluated, force=True)
    second = advisor.recommend(state, evaluated, force=True)
    assert first.preferred.player.id == evaluated[0].player.id
    assert first.overall_confidence == 84
    assert first.model_used == "gpt-5.6-sol"
    assert first.reasoning_effort == "low"
    assert first.is_fallback is False
    assert first.configured_model == "gpt-5.6-terra"
    assert first.configured_timeout_seconds == 25
    assert second.preferred.player.id == first.preferred.player.id
    assert responses.calls == 1


def test_missing_key_uses_no_network(service):
    state, evaluated, session_factory = setup_state(service)
    with pytest.raises(AdvisorUnavailable, match="OPENAI_API_KEY"):
        OpenAIStrategicAdvisor(session_factory, api_key=None).recommend(
            state, evaluated, force=True
        )


def test_invented_or_wrong_candidate_is_rejected(service):
    state, evaluated, session_factory = setup_state(service)
    ids = [item.player.id for item in evaluated[:4]] + ["invented-player"]
    advisor = OpenAIStrategicAdvisor(
        session_factory,
        api_key="test-value",
        client=SimpleNamespace(responses=FakeResponses(valid_output(ids))),
    )
    with pytest.raises(AdvisorValidationError, match="outside") as error:
        advisor.recommend(state, evaluated, force=True)
    assert error.value.category == "candidate_not_allowed"


def test_drafted_player_from_stale_candidate_list_is_rejected(service):
    draft_service, draft_id, session_factory = service
    state = draft_service.get_state(draft_id)
    stale = BaselinePlayerEvaluator(simulations=50).evaluate(state, state.available_players)
    drafted_id = stale[0].player.id
    draft_service.make_pick(draft_id, drafted_id)
    current = draft_service.get_state(draft_id)
    ids = [item.player.id for item in stale[:5]]
    advisor = OpenAIStrategicAdvisor(
        session_factory,
        api_key="test-value",
        client=SimpleNamespace(responses=FakeResponses(valid_output(ids))),
    )
    with pytest.raises(AdvisorValidationError, match="outside"):
        advisor.recommend(current, stale, force=True)


@pytest.mark.parametrize("error", [TimeoutError("timeout"), RuntimeError("rate limit")])
def test_timeout_and_provider_errors_fall_back(service, error):
    state, evaluated, session_factory = setup_user_turn(service)
    primary = OpenAIStrategicAdvisor(
        session_factory,
        api_key="test-value",
        client=SimpleNamespace(responses=FakeResponses(error=error)),
    )
    result = ResilientStrategicAdvisor(primary, OfflineStrategicAdvisor()).recommend(state, evaluated)
    assert result.source == "AI unavailable — quantitative fallback"
    assert result.preferred is not None
    assert result.is_fallback is True
    assert result.fallback_reason == f"AI request failed ({type(error).__name__})."
    assert result.configured_model == "gpt-5.6-terra"
    assert result.configured_timeout_seconds == 25
    assert result.reasoning_effort == "low"


def test_automatic_request_waits_for_our_turn_and_is_cached(service):
    draft_service, draft_id, session_factory = service
    state = draft_service.get_state(draft_id)
    if state.current_team_id == state.config.draft.our_team_id:
        draft_service.make_pick(draft_id, state.available_players[0].id)
        state = draft_service.get_state(draft_id)
    evaluated = BaselinePlayerEvaluator(simulations=100).evaluate(
        state, state.available_players
    )
    responses = FakeResponses(valid_output([item.player.id for item in evaluated[:5]]))
    advisor = OpenAIStrategicAdvisor(
        session_factory,
        api_key="test-value",
        client=SimpleNamespace(responses=responses),
    )
    with pytest.raises(AdvisorUnavailable, match="only when our team"):
        advisor.recommend(state, evaluated)
    assert responses.calls == 0

    state, evaluated, _ = setup_user_turn(service)
    responses.output = valid_output([item.player.id for item in evaluated[:5]])
    first = advisor.recommend(state, evaluated)
    second = advisor.recommend(state, evaluated)
    assert first.preferred.player.id == second.preferred.player.id
    assert responses.calls == 1


def test_failed_attempt_is_cached_until_explicit_retry(service):
    state, evaluated, session_factory = setup_user_turn(service)
    responses = FakeResponses(error=TimeoutError("timeout"))
    primary = OpenAIStrategicAdvisor(
        session_factory,
        api_key="test-value",
        client=SimpleNamespace(responses=responses),
    )
    resilient = ResilientStrategicAdvisor(primary, OfflineStrategicAdvisor())
    first = resilient.recommend(state, evaluated)
    second = resilient.recommend(state, evaluated)
    assert first.is_fallback is True
    assert second.is_fallback is True
    assert "Use Try AI again" in second.fallback_reason
    assert responses.calls == 1

    responses.error = None
    responses.output = valid_output([item.player.id for item in evaluated[:5]])
    retried = primary.recommend(
        state, evaluated, force=True, persist=True, use_cache=False
    )
    assert retried.is_fallback is False
    assert responses.calls == 2


def test_schema_rejects_invalid_categories_and_json():
    with pytest.raises(Exception):
        LLMAdvisorOutput.model_validate_json("not json")
    with pytest.raises(Exception):
        LLMAdvisorOutput.model_validate({
            "recommendations": [], "preferred_player_id": "x",
            "overall_confidence": .5, "reason": "x", "next_pick_strategy": "x",
        })


def test_diagnostic_bypasses_cache_and_reports_configuration(service):
    state, evaluated, session_factory = setup_state(service)
    responses = FakeResponses(valid_output([item.player.id for item in evaluated[:5]]))
    advisor = OpenAIStrategicAdvisor(
        session_factory,
        api_key="test-value",
        timeout_seconds=30,
        reasoning_effort="low",
        client=SimpleNamespace(responses=responses),
    )
    advisor.recommend(state, evaluated, force=True)
    diagnostic = advisor.diagnose(state, evaluated)
    assert responses.calls == 2
    assert diagnostic.success is True
    assert diagnostic.configured_model == "gpt-5.6-terra"
    assert diagnostic.model_used == "gpt-5.6-sol"
    assert diagnostic.reasoning_effort == "low"
    assert diagnostic.timeout_seconds == 30
    assert diagnostic.max_retries == 0
    assert diagnostic.structured_output_valid is True


def test_timeout_diagnostic_distinguishes_read_from_connect_timeout(service):
    state, evaluated, session_factory = setup_state(service)
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    read_error = APITimeoutError(request=request)
    read_error.__cause__ = httpx.ReadTimeout("read timed out", request=request)
    advisor = OpenAIStrategicAdvisor(
        session_factory,
        api_key="test-value",
        timeout_seconds=30,
        client=SimpleNamespace(responses=FakeResponses(error=read_error)),
    )
    diagnostic = advisor.diagnose(state, evaluated)
    assert diagnostic.success is False
    assert diagnostic.failure_category == "timeout.read"
    assert diagnostic.exception_type == "APITimeoutError"
    assert diagnostic.structured_output_valid is False

    connect_error = APITimeoutError(request=request)
    connect_error.__cause__ = httpx.ConnectTimeout("connect timed out", request=request)
    assert classify_openai_failure(connect_error) == "timeout.connect"


def test_diagnostic_reports_missing_structured_output_subtype(service):
    state, evaluated, session_factory = setup_state(service)
    responses = FakeResponses(output=None)
    advisor = OpenAIStrategicAdvisor(
        session_factory,
        api_key="test-value",
        timeout_seconds=30,
        client=SimpleNamespace(responses=responses),
    )
    diagnostic = advisor.diagnose(state, evaluated)
    assert diagnostic.success is False
    assert diagnostic.failure_category == "structured_output.missing_parsed_output"
    assert diagnostic.model_used == "gpt-5.6-sol"
    assert diagnostic.response_status == "completed"


def test_dynamic_schema_limits_all_player_ids_to_candidates(service):
    state, evaluated, session_factory = setup_state(service)
    advisor = OpenAIStrategicAdvisor(session_factory, api_key="test-value")
    candidates = evaluated[:20]
    schema = advisor._response_format(candidates).model_json_schema()
    serialized = str(schema)
    for candidate in candidates:
        assert candidate.player.id in serialized
    assert "invented-player" not in serialized


def test_missing_output_distinguishes_incomplete_and_refusal(service):
    _, _, session_factory = setup_state(service)
    advisor = OpenAIStrategicAdvisor(session_factory, api_key="test-value")
    incomplete = SimpleNamespace(
        model="gpt-5.6-terra",
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output=[],
    )
    incomplete_error = advisor._missing_output_error(incomplete)
    assert classify_openai_failure(incomplete_error) == (
        "structured_output.incomplete_max_output_tokens"
    )
    assert incomplete_error.response_status == "incomplete"

    refusal = SimpleNamespace(
        model="gpt-5.6-terra",
        status="completed",
        output=[SimpleNamespace(
            type="message",
            content=[SimpleNamespace(type="refusal")],
        )],
    )
    refusal_error = advisor._missing_output_error(refusal)
    assert classify_openai_failure(refusal_error) == "structured_output.refusal"
