"""The audit log is append-only and written on every path.

An absent entry must mean "did not happen", so denials, escalations and adapter
failures are all required to leave a record.
"""

from timeo.domain.enums import ActionStatus, OperatingMode, StepType
from timeo.gateway import ActionGateway
from timeo.policy import PolicyEngine


def _dispatch(registry, audit, tenant, property_, request, mode):
    tenant.default_mode = mode
    gw = ActionGateway(registry=registry, policy=PolicyEngine(), audit=audit)
    return gw, gw.dispatch(request, tenant=tenant, property_=property_)


def test_allow_path_logs_policy_check_and_tool_call(
    registry, audit, tenant, property_, make_request
):
    _dispatch(
        registry, audit, tenant, property_,
        make_request("send_message", body="hi"), OperatingMode.AUTONOMOUS,
    )
    steps = [e.step_type for e in audit.entries()]
    assert StepType.POLICY_CHECK in steps
    assert StepType.TOOL_CALL in steps


def test_denial_is_logged(registry, audit, tenant, property_, make_request):
    _, outcome = _dispatch(
        registry, audit, tenant, property_,
        make_request("delete_reservation", reservation_id="r1"), OperatingMode.AUTONOMOUS,
    )
    entry = next(e for e in audit.entries() if e.audit_id == outcome.audit_id)
    assert entry.payload["status"] == ActionStatus.DENIED.value
    assert entry.rules_fired


def test_escalation_and_approval_are_distinct_entries(
    registry, audit, tenant, property_, make_request
):
    gw, outcome = _dispatch(
        registry, audit, tenant, property_,
        make_request("issue_refund", amount_usd=250), OperatingMode.AUTONOMOUS,
    )
    gw.resolve_escalation(outcome.escalation_id, approved=True, resolved_by="ops@casa")

    steps = [e.step_type for e in audit.entries()]
    assert steps.count(StepType.APPROVAL) == 1
    assert StepType.TOOL_CALL in steps


def test_every_entry_carries_mode_and_decision(
    registry, audit, tenant, property_, make_request
):
    _dispatch(
        registry, audit, tenant, property_,
        make_request("issue_refund", amount_usd=250), OperatingMode.SHADOW,
    )
    for entry in audit.entries():
        assert entry.mode is OperatingMode.SHADOW
        assert entry.decision is not None


def test_entries_are_scoped_by_tenant(audit, context):
    from timeo.domain.entities import TenantContext

    audit.record(context=context, step_type=StepType.REASONING)
    audit.record(context=TenantContext(tenant_id="other"), step_type=StepType.REASONING)

    assert len(audit.entries(tenant_id="t1")) == 1
    assert len(audit.entries()) == 2


def test_payload_snapshot_is_not_affected_by_later_mutation(audit, context):
    payload = {"body": "original"}
    entry = audit.record(context=context, step_type=StepType.REASONING, payload=payload)

    payload["body"] = "mutated afterwards"

    assert entry.payload["body"] == "original"


def test_history_cannot_be_mutated_through_the_returned_sequence(audit, context):
    audit.record(context=context, step_type=StepType.REASONING)
    snapshot = audit.entries()

    assert isinstance(snapshot, tuple)  # no append/remove available
    assert len(audit) == 1


def test_cost_accounting_accumulates(audit, context):
    from timeo.domain.entities import CostRecord

    audit.record(context=context, step_type=StepType.REASONING, cost=CostRecord(usd=0.012))
    audit.record(context=context, step_type=StepType.REASONING, cost=CostRecord(usd=0.008))

    assert audit.total_cost_usd(tenant_id="t1") == 0.02
