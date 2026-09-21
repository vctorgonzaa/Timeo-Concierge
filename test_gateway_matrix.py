"""The mode x decision matrix.

The property that matters: exactly one cell — AUTONOMOUS + ALLOW — produces a
real external effect. Every test here asserts on the mock adapter's `executed`
list, which is the seam a real side effect would have to cross.
"""

import pytest

from timeo.domain.enums import ActionStatus, Decision, OperatingMode
from timeo.gateway import ActionGateway, AdapterRegistry, MockAdapter
from timeo.policy import PolicyEngine


@pytest.fixture
def in_mode(registry, audit, tenant, property_):
    """Dispatch one request with the tenant pinned to a given mode."""

    def _dispatch(mode: OperatingMode, request):
        tenant.default_mode = mode
        gw = ActionGateway(registry=registry, policy=PolicyEngine(), audit=audit)
        outcome = gw.dispatch(request, tenant=tenant, property_=property_)
        return gw, outcome

    return _dispatch


def _adapter(registry, name) -> MockAdapter:
    return registry.get(name)


# -- ALLOW column ----------------------------------------------------------


def test_simulation_allow_uses_mock_only(in_mode, registry, make_request):
    _, outcome = in_mode(OperatingMode.SIMULATION, make_request("send_message", body="hi"))

    assert outcome.status is ActionStatus.SIMULATED
    assert outcome.had_external_effect is False
    assert _adapter(registry, "send_message").executed == []


def test_shadow_allow_proposes_with_synthetic_result(in_mode, registry, make_request):
    """Shadow must hand the agent loop a usable result without sending anything."""
    _, outcome = in_mode(OperatingMode.SHADOW, make_request("send_message", body="hi"))

    assert outcome.status is ActionStatus.PROPOSED
    assert outcome.had_external_effect is False
    assert outcome.result, "shadow must return a synthetic result to continue reasoning"
    assert _adapter(registry, "send_message").executed == []


def test_autonomous_allow_executes_for_real(in_mode, registry, make_request):
    _, outcome = in_mode(OperatingMode.AUTONOMOUS, make_request("send_message", body="hi"))

    assert outcome.status is ActionStatus.EXECUTED
    assert outcome.had_external_effect is True
    assert len(_adapter(registry, "send_message").executed) == 1


# -- ESCALATE column -------------------------------------------------------


@pytest.mark.parametrize(
    "mode,expected",
    [
        (OperatingMode.SIMULATION, ActionStatus.ESCALATED),
        (OperatingMode.SHADOW, ActionStatus.PROPOSED),
        (OperatingMode.AUTONOMOUS, ActionStatus.ESCALATED),
    ],
)
def test_escalate_never_executes(in_mode, registry, make_request, mode, expected):
    _, outcome = in_mode(mode, make_request("issue_refund", amount_usd=250))

    assert outcome.status is expected
    assert outcome.had_external_effect is False
    assert outcome.verdict.decision is Decision.ESCALATE
    assert _adapter(registry, "issue_refund").executed == []


def test_autonomous_escalation_opens_an_approval_task(in_mode, make_request):
    gw, outcome = in_mode(OperatingMode.AUTONOMOUS, make_request("issue_refund", amount_usd=250))

    assert outcome.escalation_id is not None
    pending = gw.pending_escalations("t1")
    assert len(pending) == 1
    assert pending[0].action == "issue_refund"
    assert pending[0].reasons


# -- DENY column -----------------------------------------------------------


@pytest.mark.parametrize(
    "mode", [OperatingMode.SIMULATION, OperatingMode.SHADOW, OperatingMode.AUTONOMOUS]
)
def test_deny_blocks_in_every_mode(in_mode, registry, make_request, mode):
    _, outcome = in_mode(mode, make_request("delete_reservation", reservation_id="r1"))

    assert outcome.status is ActionStatus.DENIED
    assert outcome.had_external_effect is False
    assert _adapter(registry, "delete_reservation").executed == []


def test_policy_is_live_in_simulation(in_mode, make_request):
    """Simulation rehearses the decision path, so policy tuning is testable there."""
    _, outcome = in_mode(OperatingMode.SIMULATION, make_request("issue_refund", amount_usd=250))
    assert outcome.verdict.decision is Decision.ESCALATE
    assert "refund_over_threshold" in outcome.verdict.rule_summary


# -- failure paths ---------------------------------------------------------


def test_unknown_action_is_refused_not_ignored(in_mode, make_request):
    _, outcome = in_mode(OperatingMode.AUTONOMOUS, make_request("teleport_guest"))

    assert outcome.status is ActionStatus.DENIED
    assert "unknown_action" in outcome.verdict.rule_summary


def test_adapter_failure_is_reported_not_swallowed(audit, tenant, property_, make_request):
    class Exploding:
        name = "send_message"

        def execute(self, request):
            raise RuntimeError("upstream 503")

        def simulate(self, request):
            return {"ok": True}

    registry = AdapterRegistry()
    registry.register(Exploding())
    tenant.default_mode = OperatingMode.AUTONOMOUS
    gw = ActionGateway(registry=registry, policy=PolicyEngine(), audit=audit)

    outcome = gw.dispatch(
        make_request("send_message", body="hi"), tenant=tenant, property_=property_
    )

    assert outcome.status is ActionStatus.FAILED
    assert outcome.had_external_effect is False
    assert "upstream 503" in outcome.error


# -- approval flow ---------------------------------------------------------


def test_approval_is_recorded_separately_from_execution(in_mode, make_request):
    gw, outcome = in_mode(OperatingMode.AUTONOMOUS, make_request("issue_refund", amount_usd=250))

    task = gw.resolve_escalation(outcome.escalation_id, approved=True, resolved_by="ops@casa")

    assert task.approved is True
    assert task.resolved_by == "ops@casa"
    assert task.resolved_at is not None
    assert gw.pending_escalations("t1") == []


def test_unknown_escalation_id_raises_a_named_error(in_mode, make_request):
    from timeo.domain.errors import UnknownEscalationError

    gw, _ = in_mode(OperatingMode.AUTONOMOUS, make_request("issue_refund", amount_usd=250))

    with pytest.raises(UnknownEscalationError):
        gw.resolve_escalation("does-not-exist", approved=True, resolved_by="ops@casa")


def test_double_approval_is_refused(in_mode, make_request):
    """A second decision would append an APPROVAL entry no human actually made."""
    from timeo.domain.errors import EscalationAlreadyResolvedError

    gw, outcome = in_mode(OperatingMode.AUTONOMOUS, make_request("issue_refund", amount_usd=250))
    gw.resolve_escalation(outcome.escalation_id, approved=True, resolved_by="ops@casa")

    with pytest.raises(EscalationAlreadyResolvedError):
        gw.resolve_escalation(outcome.escalation_id, approved=False, resolved_by="someone@else")
