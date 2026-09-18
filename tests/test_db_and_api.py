import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import date

import pytest
from fastapi.testclient import TestClient

from policy_update.change_management.changeset_schema import (
    ChangeCategory,
    ChangeItem,
    ChangeSet,
    ChangeSetStatus,
    RiskLevel,
)
from policy_update.change_management.db import init_db, make_engine, make_session_factory, session_scope
from policy_update.change_management.repository import load_changeset, save_changeset


@pytest.fixture()
def session_factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/test.db")
    init_db(engine)
    return make_session_factory(engine)


def _sample_changeset() -> ChangeSet:
    changeset = ChangeSet(
        company_id="company-demo",
        baseline_formula_version=2,
        baseline_workbook_version="workbook-v5",
        effective_from=date(2026, 10, 1),
        scope=None,
        risk_level=RiskLevel.MEDIUM,
    )
    item = ChangeItem(
        changeset_id=changeset.id,
        category=ChangeCategory.PARAMETER_CHANGE,
        field_path="Allowances[code=MEAL].amount",
        old_value=730000,
        proposed_value=900000,
        reason="Meal allowance increase",
        evidence_refs=("policy-v2#3.2.MEAL_ALLOWANCE",),
        effective_date=date(2026, 10, 1),
        confidence=0.95,
    )
    changeset.add_item(item)
    return changeset


def test_save_and_load_round_trip(session_factory):
    changeset = _sample_changeset()
    with session_scope(session_factory) as session:
        save_changeset(session, changeset)

    with session_scope(session_factory) as session:
        loaded = load_changeset(session, changeset.id)
    assert loaded is not None
    assert loaded.company_id == changeset.company_id
    assert loaded.status is ChangeSetStatus.DRAFT
    assert len(loaded.items) == 1
    assert loaded.items[0].field_path == "Allowances[code=MEAL].amount"
    assert loaded.items[0].old_value == 730000


def test_load_missing_changeset_returns_none(session_factory):
    with session_scope(session_factory) as session:
        assert load_changeset(session, "does-not-exist") is None


@pytest.fixture()
def api_client(monkeypatch, tmp_path):
    from policy_update.change_management import db, review_api

    engine = make_engine(f"sqlite:///{tmp_path}/api_test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    monkeypatch.setattr(review_api, "make_session_factory", lambda: factory)
    monkeypatch.setattr(review_api, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(review_api.router)
    client = TestClient(app)

    changeset = _sample_changeset()
    changeset.status = ChangeSetStatus.IN_REVIEW
    with session_scope(factory) as session:
        save_changeset(session, changeset)

    return client, changeset.id, str(tmp_path / "audit.log")


def test_review_api_approve_requires_approver_role(api_client):
    client, changeset_id, _ = api_client
    resp = client.post(
        f"/changesets/{changeset_id}/review",
        json={"decision": "APPROVE", "note": ""},
        headers={"X-User-Id": "reviewer-1", "X-User-Role": "reviewer"},
    )
    assert resp.status_code == 409, resp.text
    assert "cannot move" in resp.json()["detail"]


def test_review_api_approve_succeeds_for_approver(api_client):
    client, changeset_id, audit_path = api_client
    resp = client.post(
        f"/changesets/{changeset_id}/review",
        json={"decision": "APPROVE", "note": "looks good"},
        headers={"X-User-Id": "approver-1", "X-User-Role": "approver"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "APPROVED"

    audit_content = Path(audit_path).read_text(encoding="utf-8")
    assert "policy_update.review_decision" in audit_content
    assert "approver-1" in audit_content


def test_review_api_reject_requires_note(api_client):
    client, changeset_id, _ = api_client
    resp = client.post(
        f"/changesets/{changeset_id}/review",
        json={"decision": "REJECT", "note": ""},
        headers={"X-User-Id": "reviewer-1", "X-User-Role": "reviewer"},
    )
    assert resp.status_code == 422


def test_review_api_missing_auth_headers_rejected(api_client):
    client, changeset_id, _ = api_client
    resp = client.post(
        f"/changesets/{changeset_id}/review",
        json={"decision": "APPROVE", "note": "x"},
    )
    assert resp.status_code == 401
