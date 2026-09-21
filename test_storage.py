"""Durability and the guarantees the storage layer is responsible for."""

import pytest

from timeo.domain.entities import EscalationTask, TenantContext
from timeo.domain.enums import OperatingMode, StepType
from timeo.domain.errors import TenantIsolationError
from timeo.policy.rules import PolicySettings
from timeo.storage import (
    SqliteAuditLog,
    SqliteEscalationStore,
    SqlitePolicyStore,
    connect,
)


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def store(conn):
    return SqliteEscalationStore(conn)


def _task(tenant="t1", **kw):
    return EscalationTask(
        tenant_id=tenant,
        property_id="p1",
        conversation_id="c1",
        action="issue_refund",
        params={"amount_usd": 250},
        reasons=["refund_over_threshold: over the $50 limit"],
        mode=OperatingMode.AUTONOMOUS,
        **kw,
    )


# -- escalations -----------------------------------------------------------


def test_task_round_trips_with_its_params_and_reasons(store):
    saved = store.add(_task())
    loaded = store.get(saved.escalation_id)

    assert loaded.params == {"amount_usd": 250}
    assert loaded.reasons == ["refund_over_threshold: over the $50 limit"]
    assert loaded.mode is OperatingMode.AUTONOMOUS
    assert loaded.approved is None


def test_queue_survives_a_reconnect(tmp_path):
    """The whole point: a restart between asking and answering loses nothing."""
    db = tmp_path / "timeo.db"
    first = SqliteEscalationStore(connect(db))
    task = first.add(_task())

    reopened = SqliteEscalationStore(connect(db))

    assert len(reopened.pending("t1")) == 1
    assert reopened.get(task.escalation_id).action == "issue_refund"


def test_pending_excludes_resolved(store):
    task = store.add(_task())
    assert len(store.pending("t1")) == 1

    task.approved = True
    task.resolved_by = "ops@casa"
    store.save(task)

    assert store.pending("t1") == []


def test_pending_is_scoped_by_tenant(store):
    store.add(_task("t1"))
    store.add(_task("t2"))

    assert len(store.pending("t1")) == 1
    assert len(store.pending("t2")) == 1


def test_cross_tenant_fetch_raises(store):
    task = store.add(_task("t1"))

    with pytest.raises(TenantIsolationError):
        store.get_scoped(task.escalation_id, "t2")


def test_scoped_fetch_of_a_missing_task_is_a_plain_miss(store):
    assert store.get_scoped("nope", "t1") is None


# -- audit -----------------------------------------------------------------


def test_audit_entries_are_append_only_at_the_database(conn):
    """Enforced by trigger, so no application bug can revise history."""
    audit = SqliteAuditLog(conn)
    audit.record(context=TenantContext(tenant_id="t1"), step_type=StepType.REASONING)

    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE audit_entries SET actor = 'forged'")

    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM audit_entries")


def test_audit_survives_a_reconnect(tmp_path):
    db = tmp_path / "timeo.db"
    SqliteAuditLog(connect(db)).record(
        context=TenantContext(tenant_id="t1"), step_type=StepType.TOOL_CALL
    )

    assert len(SqliteAuditLog(connect(db)).entries(tenant_id="t1")) == 1


def test_audit_snapshot_is_frozen_against_later_mutation(conn):
    audit = SqliteAuditLog(conn)
    payload = {"body": "original"}
    audit.record(
        context=TenantContext(tenant_id="t1"), step_type=StepType.REASONING, payload=payload
    )
    payload["body"] = "mutated afterwards"

    assert audit.entries(tenant_id="t1")[0].payload["body"] == "original"


def test_audit_can_be_filtered_by_conversation(conn):
    audit = SqliteAuditLog(conn)
    ctx = TenantContext(tenant_id="t1")
    audit.record(context=ctx, step_type=StepType.REASONING, conversation_id="c1")
    audit.record(context=ctx, step_type=StepType.REASONING, conversation_id="c2")

    assert len(audit.entries(tenant_id="t1", conversation_id="c1")) == 1


def test_audit_cost_is_summed_in_the_database(conn):
    from timeo.domain.entities import CostRecord

    audit = SqliteAuditLog(conn)
    ctx = TenantContext(tenant_id="t1")
    audit.record(context=ctx, step_type=StepType.REASONING, cost=CostRecord(usd=0.012))
    audit.record(context=ctx, step_type=StepType.REASONING, cost=CostRecord(usd=0.008))

    assert audit.total_cost_usd(tenant_id="t1") == 0.02


# -- policy scoping --------------------------------------------------------


def test_unconfigured_tenant_still_gets_the_default_ruleset(conn):
    """"No row" must not mean "no policy"."""
    from timeo.domain.entities import ActionRequest
    from timeo.domain.enums import Decision

    engine = SqlitePolicyStore(conn).engine_for("never-configured")
    verdict = engine.evaluate(
        ActionRequest(
            action="issue_refund",
            params={"amount_usd": 500},
            context=TenantContext(tenant_id="never-configured"),
        )
    )
    assert verdict.decision is Decision.ESCALATE


def test_property_scope_can_tighten_its_tenant_limit(conn):
    from timeo.domain.entities import ActionRequest
    from timeo.domain.enums import Decision

    store = SqlitePolicyStore(conn)
    store.set_settings("t1", PolicySettings(refund_escalation_threshold_usd=500.0))
    store.set_settings(
        "t1", PolicySettings(refund_escalation_threshold_usd=10.0), property_id="p1"
    )

    request = ActionRequest(
        action="issue_refund",
        params={"amount_usd": 100},
        context=TenantContext(tenant_id="t1"),
    )

    assert store.engine_for("t1").evaluate(request).decision is Decision.ALLOW
    assert (
        store.engine_for("t1", property_id="p1").evaluate(request).decision is Decision.ESCALATE
    )


def test_property_scope_cannot_loosen_what_the_tenant_forbade(conn):
    """A prohibition any property could switch off would not be a prohibition."""
    from timeo.domain.entities import ActionRequest
    from timeo.domain.enums import Decision

    store = SqlitePolicyStore(conn)
    store.set_settings(
        "t1", PolicySettings(forbidden_actions=frozenset({"issue_refund"}))
    )
    store.set_settings(
        "t1", PolicySettings(forbidden_actions=frozenset()), property_id="p1"
    )

    request = ActionRequest(
        action="issue_refund", params={"amount_usd": 5}, context=TenantContext(tenant_id="t1")
    )

    assert store.engine_for("t1", property_id="p1").evaluate(request).decision is Decision.DENY


def test_settings_round_trip(conn):
    store = SqlitePolicyStore(conn)
    store.set_settings("t1", PolicySettings(refund_escalation_threshold_usd=125.5))

    assert store.get_settings("t1").refund_escalation_threshold_usd == 125.5


def test_settings_update_in_place(conn):
    store = SqlitePolicyStore(conn)
    store.set_settings("t1", PolicySettings(refund_escalation_threshold_usd=50.0))
    store.set_settings("t1", PolicySettings(refund_escalation_threshold_usd=75.0))

    assert store.get_settings("t1").refund_escalation_threshold_usd == 75.0
