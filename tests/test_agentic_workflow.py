from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic.api import router
from agentic.loop_guard import LoopGuard
from agentic.models import AgentDecision, PlanProposal
from agentic.models import AgentPhase
from agentic.react_runner import ReActRunner
from agentic.token_meter import TokenMeter
from policy_update.change_management.db import init_db, make_engine, make_session_factory


def _plan() -> dict:
    return {
        "goal": "Update a reviewed payroll configuration",
        "summary_for_hr": "Apply one reviewed change.",
        "assumptions": [],
        "risks": [],
        "steps": [
            {
                "id": "step-1",
                "title": "Update parameter",
                "description": "Apply the approved parameter update.",
                "allowed_tools": ["update_parameter"],
                "expected_result": "The value is updated.",
            }
        ],
    }


def test_case_api_enforces_review_before_execution(monkeypatch, tmp_path):
    import agentic.api as api

    engine = make_engine(f"sqlite:///{tmp_path}/agentic.db")
    init_db(engine)
    factory = make_session_factory(engine)
    monkeypatch.setattr(api, "make_session_factory", lambda: factory)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    headers = {"X-User-Id": "hr-1"}

    created = client.post("/cases", headers=headers, json={"requester_id": "hr-1", "brief": "Increase meal allowance"})
    assert created.status_code == 201, created.text
    case = created.json()
    assert case["status"] == "RECEIVED"
    assert case["changeset_id"].startswith("changeset-pending-")

    denied = client.post(f"/cases/{case['case_id']}/runs", headers=headers, json={})
    assert denied.status_code == 409

    submitted = client.post(f"/cases/{case['case_id']}/plans", headers=headers, json=_plan())
    assert submitted.status_code == 201, submitted.text
    assert client.get(f"/cases/{case['case_id']}").json()["status"] == "REVIEW"

    approved = client.post(f"/cases/{case['case_id']}/plan-decision", headers=headers, json={"approved": True})
    assert approved.status_code == 200, approved.text
    assert approved.json()["case"]["status"] == "APPROVED"

    run = client.post(f"/cases/{case['case_id']}/runs", headers=headers, json={"max_steps": 5})
    assert run.status_code == 201, run.text
    finished = client.post(
        f"/cases/runs/{run.json()['run_id']}/finish", headers=headers,
        json={"status": "COMPLETED", "completed_step_ids": ["step-1"]},
    )
    assert finished.status_code == 200, finished.text
    assert client.get(f"/cases/{case['case_id']}").json()["status"] == "DONE"


def test_token_report_is_aggregated_by_case(monkeypatch, tmp_path):
    import agentic.api as api

    engine = make_engine(f"sqlite:///{tmp_path}/tokens.db")
    init_db(engine)
    factory = make_session_factory(engine)
    monkeypatch.setattr(api, "make_session_factory", lambda: factory)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    headers = {"X-User-Id": "hr-1"}
    case_id = client.post("/cases", headers=headers, json={"requester_id": "hr-1", "brief": "test"}).json()["case_id"]
    for phase, input_tokens, output_tokens in [("plan", 10, 5), ("react", 20, 7)]:
        response = client.post(
            f"/cases/{case_id}/token-usage", headers=headers,
            json={
                "model": "gemma-4-31b-it", "phase": phase, "input_tokens": input_tokens,
                "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens,
            },
        )
        assert response.status_code == 201, response.text
    report = client.get(f"/cases/{case_id}/token-report")
    assert report.status_code == 200
    assert report.json()["total_tokens"] == 42
    assert report.json()["calls"] == 2


def test_chat_submits_a_structured_plan_but_never_auto_approves(monkeypatch, tmp_path):
    import agentic.api as api

    engine = make_engine(f"sqlite:///{tmp_path}/chat.db")
    init_db(engine)
    factory = make_session_factory(engine)
    monkeypatch.setattr(api, "make_session_factory", lambda: factory)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[api.get_agent_client] = lambda: _SequenceClient([
        {"decision": "present_plan", "message": "I have enough information.", "missing_information": []},
        _plan(),
    ])
    client = TestClient(app)
    headers = {"X-User-Id": "hr-1"}
    case_id = client.post("/cases", headers=headers, json={"requester_id": "hr-1", "brief": "test"}).json()["case_id"]

    reply = client.post(f"/cases/{case_id}/messages", headers=headers, json={"content": "Increase meal allowance"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["decision"]["decision"] == "present_plan"
    assert reply.json()["plan"]["version"] == 1
    assert reply.json()["case"]["status"] == "REVIEW"


class _SequenceClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def complete_structured(self, **_):
        return self.payloads.pop(0), {
            "model": "gemma-4-31b-it", "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
        }


def test_react_runner_stops_repeated_actions_before_second_execution():
    decision = {
        "decision": "tool_call", "plan_step_id": "step-1", "reason": "try update",
        "tool_name": "update_parameter", "tool_input": {"value": 1}, "mark_step_complete": False,
    }
    calls = []
    result = ReActRunner(
        client=_SequenceClient([decision, decision]),
        tools={"update_parameter": lambda payload: calls.append(payload) or {"ok": True}},
        loop_guard=LoopGuard(max_same_action=2),
    ).run(plan=PlanProposal.model_validate(_plan()), initial_context={})
    assert result.status == "NEEDS_REVIEW"
    assert "identical tool action" in result.stop_reason
    assert len(calls) == 1


def test_react_runner_completes_only_after_approved_step_is_done():
    payloads = [
        {
            "decision": "tool_call", "plan_step_id": "step-1", "reason": "apply",
            "tool_name": "update_parameter", "tool_input": {"value": 1}, "mark_step_complete": True,
        },
        {"decision": "complete", "plan_step_id": "step-1", "reason": "done", "mark_step_complete": True},
    ]
    result = ReActRunner(
        client=_SequenceClient(payloads), tools={"update_parameter": lambda _: {"ok": True}},
    ).run(plan=PlanProposal.model_validate(_plan()), initial_context={})
    assert result.status == "COMPLETED"
    assert result.completed_step_ids == ("step-1",)


def test_token_meter_uses_google_usage_metadata_without_estimating_tokens():
    record = TokenMeter().record_google_usage(
        case_id="case-1", run_id="run-1", phase=AgentPhase.REACT,
        provider_usage={"model": "gemma-4-31b-it", "input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
    )
    assert record.total_tokens == 15
