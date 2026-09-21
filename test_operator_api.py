"""Operator API behaviour.

The queue is where a human exercises authority over the system, so these tests
care most about who can act, what gets recorded, and what happens when two
managers reach for the same task.
"""

import pytest
from fastapi.testclient import TestClient

from timeo.api.app import Deps, create_app
from timeo.domain.entities import ActionRequest, Property, Tenant, TenantContext
from timeo.domain.enums import OperatingMode
from timeo.gateway.adapters import AdapterRegistry, MockAdapter
from timeo.gateway.gateway import ActionGateway
from timeo.storage import (
    SqliteAuditLog,
    SqliteEscalationStore,
    SqlitePolicyStore,
    connect,
)

OPS = {"X-Operator-Id": "ops@casaverde"}


@pytest.fixture
def wired():
    conn = connect(":memory:")
    escalations = SqliteEscalationStore(conn)
    audit = SqliteAuditLog(conn)
    policy = SqlitePolicyStore(conn)

    registry = AdapterRegistry()
    for name in ("send_message", "issue_refund"):
        registry.register(MockAdapter(name))

    gateway = ActionGateway(
        registry=registry,
        policy=policy.engine_for("t1"),
        audit=audit,
        escalations=escalations,
    )
    deps = Deps(conn=conn, escalations=escalations, audit=audit, policy=policy, gateway=gateway)
    return TestClient(create_app(deps)), gateway, audit


def _raise_escalation(gateway, tenant_id="t1", amount=250):
    tenant = Tenant(tenant_id=tenant_id, default_mode=OperatingMode.AUTONOMOUS)
    prop = Property(property_id="p1", tenant_id=tenant_id)
    outcome = gateway.dispatch(
        ActionRequest(
            action="issue_refund",
            params={"amount_usd": amount, "reason": "AC failure"},
            context=TenantContext(tenant_id=tenant_id, property_id="p1"),
            conversation_id="c1",
        ),
        tenant=tenant,
        property_=prop,
    )
    return outcome.escalation_id


def test_health(wired):
    client, _, _ = wired
    assert client.get("/health").json() == {"status": "ok"}


# -- the queue -------------------------------------------------------------


def test_pending_queue_lists_the_task_with_its_reason(wired):
    client, gateway, _ = wired
    _raise_escalation(gateway)

    body = client.get("/tenants/t1/escalations").json()

    assert len(body) == 1
    assert body[0]["action"] == "issue_refund"
    assert body[0]["status"] == "pending"
    assert "refund_over_threshold" in body[0]["reasons"][0]


def test_approval_records_who_decided(wired):
    client, gateway, _ = wired
    eid = _raise_escalation(gateway)

    body = client.post(
        f"/tenants/t1/escalations/{eid}/decision", params={"approved": True}, headers=OPS
    ).json()

    assert body["status"] == "approved"
    assert body["resolved_by"] == "ops@casaverde"
    assert body["resolved_at"] is not None


def test_rejection_is_recorded_too(wired):
    client, gateway, _ = wired
    eid = _raise_escalation(gateway)

    body = client.post(
        f"/tenants/t1/escalations/{eid}/decision", params={"approved": False}, headers=OPS
    ).json()

    assert body["status"] == "rejected"


def test_resolved_task_leaves_the_pending_queue(wired):
    client, gateway, _ = wired
    eid = _raise_escalation(gateway)
    client.post(
        f"/tenants/t1/escalations/{eid}/decision", params={"approved": True}, headers=OPS
    )

    assert client.get("/tenants/t1/escalations").json() == []


def test_second_decision_conflicts(wired):
    """Two managers reaching for the same task: the second is told, not ignored."""
    client, gateway, _ = wired
    eid = _raise_escalation(gateway)
    client.post(
        f"/tenants/t1/escalations/{eid}/decision", params={"approved": True}, headers=OPS
    )

    second = client.post(
        f"/tenants/t1/escalations/{eid}/decision",
        params={"approved": False},
        headers={"X-Operator-Id": "someone@else"},
    )

    assert second.status_code == 409


def test_decision_requires_an_operator_identity(wired):
    client, gateway, _ = wired
    eid = _raise_escalation(gateway)

    response = client.post(f"/tenants/t1/escalations/{eid}/decision", params={"approved": True})

    assert response.status_code == 401


def test_unknown_escalation_is_404(wired):
    client, _, _ = wired
    assert client.get("/tenants/t1/escalations/nope").status_code == 404


# -- tenant isolation ------------------------------------------------------


def test_another_tenants_task_is_not_visible(wired):
    client, gateway, _ = wired
    _raise_escalation(gateway, tenant_id="t2")

    assert client.get("/tenants/t1/escalations").json() == []


def test_cross_tenant_read_is_404_but_audited(wired):
    """Opaque to the caller, loud in the record."""
    client, gateway, audit = wired
    eid = _raise_escalation(gateway, tenant_id="t2")

    response = client.get(f"/tenants/t1/escalations/{eid}")

    assert response.status_code == 404
    attempts = [
        e for e in audit.entries(tenant_id="t1") if "cross_tenant_attempt" in e.payload
    ]
    assert len(attempts) == 1


def test_cross_tenant_approval_is_blocked(wired):
    client, gateway, _ = wired
    eid = _raise_escalation(gateway, tenant_id="t2")

    response = client.post(
        f"/tenants/t1/escalations/{eid}/decision", params={"approved": True}, headers=OPS
    )

    assert response.status_code == 404
    assert gateway.pending_escalations("t2")[0].approved is None


# -- audit and cost --------------------------------------------------------


def test_audit_trail_is_readable_per_conversation(wired):
    client, gateway, _ = wired
    _raise_escalation(gateway)

    entries = client.get("/tenants/t1/audit", params={"conversation_id": "c1"}).json()

    assert entries
    assert {e["step_type"] for e in entries} >= {"policy_check", "tool_call"}


def test_cost_endpoint_reports_spend(wired):
    client, gateway, _ = wired
    _raise_escalation(gateway)

    assert "total_usd" in client.get("/tenants/t1/cost").json()


# -- policy configuration --------------------------------------------------


def test_unconfigured_policy_reports_the_defaults_not_404(wired):
    client, _, _ = wired
    body = client.get("/tenants/t1/policy").json()

    assert body["source"] == "default"
    assert body["refund_threshold_usd"] == 50.0


def test_policy_change_is_persisted_and_audited(wired):
    client, _, audit = wired

    client.put("/tenants/t1/policy", params={"refund_threshold_usd": 200.0}, headers=OPS)
    body = client.get("/tenants/t1/policy").json()

    assert body["source"] == "configured"
    assert body["refund_threshold_usd"] == 200.0
    changes = [e for e in audit.entries(tenant_id="t1") if e.payload.get("change")]
    assert changes[0].actor == "ops@casaverde"


def test_policy_change_requires_an_operator_identity(wired):
    client, _, _ = wired
    response = client.put("/tenants/t1/policy", params={"refund_threshold_usd": 200.0})

    assert response.status_code == 401
