from __future__ import annotations

from types import SimpleNamespace

import pytest

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
        assert kwargs["text_format"] is LLMAdvisorOutput
        if self.error:
            raise self.error
        return SimpleNamespace(output_parsed=self.output)


def setup_state(service):
    draft_service, draft_id, session_factory = service
    state = draft_service.get_state(draft_id)
    evaluated = BaselinePlayerEvaluator(simulations=100).evaluate(state, state.available_players)
    return state, evaluated, session_factory


def test_valid_structured_result_is_persisted_and_cached(service):
    state, evaluated, session_factory = setup_state(service)
    responses = FakeResponses(valid_output([item.player.id for item in evaluated[:5]]))
    advisor = OpenAIStrategicAdvisor(
        session_factory, api_key="test-value", client=SimpleNamespace(responses=responses)
    )
    first = advisor.recommend(state, evaluated)
    second = advisor.recommend(state, evaluated)
    assert first.preferred.player.id == evaluated[0].player.id
    assert first.overall_confidence == 84
    assert second.preferred.player.id == first.preferred.player.id
    assert responses.calls == 1


def test_missing_key_uses_no_network(service):
    state, evaluated, session_factory = setup_state(service)
    with pytest.raises(AdvisorUnavailable, match="OPENAI_API_KEY"):
        OpenAIStrategicAdvisor(session_factory, api_key=None).recommend(state, evaluated)


def test_invented_or_wrong_candidate_is_rejected(service):
    state, evaluated, session_factory = setup_state(service)
    ids = [item.player.id for item in evaluated[:4]] + ["invented-player"]
    advisor = OpenAIStrategicAdvisor(
        session_factory,
        api_key="test-value",
        client=SimpleNamespace(responses=FakeResponses(valid_output(ids))),
    )
    with pytest.raises(AdvisorValidationError, match="outside"):
        advisor.recommend(state, evaluated)


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
    state, evaluated, session_factory = setup_state(service)
    primary = OpenAIStrategicAdvisor(
        session_factory,
        api_key="test-value",
        client=SimpleNamespace(responses=FakeResponses(error=error)),
    )
    result = ResilientStrategicAdvisor(primary, OfflineStrategicAdvisor()).recommend(state, evaluated)
    assert result.source == "AI unavailable — quantitative fallback"
    assert result.preferred is not None


def test_schema_rejects_invalid_categories_and_json():
    with pytest.raises(Exception):
        LLMAdvisorOutput.model_validate_json("not json")
    with pytest.raises(Exception):
        LLMAdvisorOutput.model_validate({
            "recommendations": [], "preferred_player_id": "x",
            "overall_confidence": .5, "reason": "x", "next_pick_strategy": "x",
        })
