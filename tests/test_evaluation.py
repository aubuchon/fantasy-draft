from __future__ import annotations

from fantasy_draft.evaluation import (
    BaselinePlayerEvaluator,
    OfflineStrategicAdvisor,
    ResilientStrategicAdvisor,
)


class FailingAdvisor:
    def recommend(self, state, evaluated):
        raise TimeoutError("model timed out")


def test_evaluator_uses_following_pick_for_survival(service):
    draft_service, draft_id, _ = service
    state = draft_service.get_state(draft_id)
    evaluated = BaselinePlayerEvaluator().evaluate(state, state.available_players)
    first = next(item for item in evaluated if item.player.id == "player-1")
    assert state.next_user_pick == 16
    assert first.survival_probability is not None
    assert first.survival_probability < 0.2


def test_failed_advisor_falls_back_without_affecting_state(service):
    draft_service, draft_id, _ = service
    state = draft_service.get_state(draft_id)
    evaluated = BaselinePlayerEvaluator().evaluate(state, state.available_players)
    advisor = ResilientStrategicAdvisor(FailingAdvisor(), OfflineStrategicAdvisor())
    result = advisor.recommend(state, evaluated)
    assert result.source == "offline baseline"
    assert result.preferred is not None
    assert draft_service.get_state(draft_id).picks == []
