"""A retried dispatch must not repeat the side effect.

Redelivery is ordinary in this system — queues retry, networks blip, agent loops
repeat themselves. Since the dominant side effect is messaging a guest, a
duplicate is something a guest would actually see.
"""

import pytest

from timeo.domain.entities import ActionRequest, TenantContext
from timeo.domain.enums import ActionStatus, Channel, OperatingMode
from timeo.gateway import ActionGateway, AdapterRegistry
from timeo.policy import PolicyEngine


@pytest.fixture
def autonomous(tenant, property_, registry, audit):
    tenant.default_mode = OperatingMode.AUTONOMOUS
    gw = ActionGateway(registry=registry, policy=PolicyEngine(), audit=audit)

    def _dispatch(request):
        return gw.dispatch(request, tenant=tenant, property_=property_)

    return gw, _dispatch


def test_replayed_request_executes_once(autonomous, registry, make_request):
    _, dispatch = autonomous
    request = make_request("send_message", body="your door code is 4417")

    first = dispatch(request)
    second = dispatch(request)

    assert len(registry.get("send_message").executed) == 1
    assert first.deduplicated is False
    assert second.deduplicated is True


def test_replay_is_transparent_to_the_caller(autonomous, make_request):
    """The retry returns the original result so the agent loop stays coherent."""
    _, dispatch = autonomous
    request = make_request("send_message", body="hi")

    first = dispatch(request)
    second = dispatch(request)

    assert second.status == first.status == ActionStatus.EXECUTED
    assert second.result == first.result


def test_replay_reports_no_external_effect(autonomous, make_request):
    """The effect belongs to the first dispatch; counting it twice is the bug."""
    _, dispatch = autonomous
    request = make_request("send_message", body="hi")

    assert dispatch(request).had_external_effect is True
    assert dispatch(request).had_external_effect is False


def test_distinct_requests_still_both_send(autonomous, registry, make_request):
    _, dispatch = autonomous
    dispatch(make_request("send_message", body="first"))
    dispatch(make_request("send_message", body="second"))

    assert len(registry.get("send_message").executed) == 2


def test_escalation_is_not_opened_twice(autonomous, make_request):
    gw, dispatch = autonomous
    request = make_request("issue_refund", amount_usd=250)

    first = dispatch(request)
    second = dispatch(request)

    assert len(gw.pending_escalations("t1")) == 1
    assert second.escalation_id == first.escalation_id


def test_failure_remains_retryable(audit, tenant, property_, make_request):
    """A transient 503 must not be frozen into a permanent failure."""

    class Flaky:
        name = "send_message"

        def __init__(self):
            self.calls = 0

        def execute(self, request):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("upstream 503")
            return {"ok": True}

        def simulate(self, request):
            return {"ok": True}

    adapter = Flaky()
    registry = AdapterRegistry()
    registry.register(adapter)
    tenant.default_mode = OperatingMode.AUTONOMOUS
    gw = ActionGateway(registry=registry, policy=PolicyEngine(), audit=audit)
    request = make_request("send_message", body="hi")

    first = gw.dispatch(request, tenant=tenant, property_=property_)
    second = gw.dispatch(request, tenant=tenant, property_=property_)

    assert first.status is ActionStatus.FAILED
    assert second.status is ActionStatus.EXECUTED
    assert second.deduplicated is False


def test_request_ids_do_not_collide_across_tenants(registry, audit, tenant, property_):
    """Two tenants reusing an id must not suppress one another's actions."""
    tenant.default_mode = OperatingMode.AUTONOMOUS
    gw = ActionGateway(registry=registry, policy=PolicyEngine(), audit=audit)

    def _request(tenant_id):
        return ActionRequest(
            action="send_message",
            params={"body": "hi"},
            context=TenantContext(tenant_id=tenant_id, channel=Channel.WHATSAPP),
            request_id="shared-id",
        )

    from timeo.domain.entities import Tenant

    gw.dispatch(_request("t1"), tenant=tenant, property_=property_)
    other = Tenant(tenant_id="t2", default_mode=OperatingMode.AUTONOMOUS)
    gw.dispatch(_request("t2"), tenant=other)

    assert len(registry.get("send_message").executed) == 2


def test_suppression_is_audited(autonomous, audit, make_request):
    """An absent entry means "did not happen"; a suppressed retry did happen."""
    _, dispatch = autonomous
    request = make_request("send_message", body="hi")
    dispatch(request)
    dispatch(request)

    suppressed = [
        e for e in audit.entries() if e.payload.get("status") == "duplicate_suppressed"
    ]
    assert len(suppressed) == 1
    assert suppressed[0].payload["original_status"] == ActionStatus.EXECUTED.value
