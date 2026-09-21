"""Policy is deterministic and independent of rule ordering."""

from timeo.domain.entities import ActionRequest, TenantContext
from timeo.domain.enums import Decision
from timeo.policy import PolicyEngine, PolicySettings, ScopedPolicy


def _engine(**settings):
    return PolicyEngine([ScopedPolicy(settings=PolicySettings(**settings))])


def test_small_refund_is_allowed(make_request):
    verdict = _engine().evaluate(make_request("issue_refund", amount_usd=25))
    assert verdict.decision is Decision.ALLOW


def test_refund_over_threshold_escalates(make_request):
    """Section 5.3: discretionary refunds above $50 need a human."""
    verdict = _engine().evaluate(make_request("issue_refund", amount_usd=200))
    assert verdict.decision is Decision.ESCALATE
    assert "refund_over_threshold" in verdict.rule_summary


def test_threshold_is_configurable_per_scope(make_request):
    request = make_request("issue_refund", amount_usd=120)
    assert _engine().evaluate(request).decision is Decision.ESCALATE
    assert (
        _engine(refund_escalation_threshold_usd=500.0).evaluate(request).decision
        is Decision.ALLOW
    )


def test_unparseable_amount_escalates(make_request):
    """Ambiguous money goes to a human rather than through."""
    verdict = _engine().evaluate(make_request("issue_refund", amount_usd="a lot"))
    assert verdict.decision is Decision.ESCALATE


def test_safety_report_always_escalates(make_request):
    verdict = _engine().evaluate(make_request("file_safety_report", detail="gas smell"))
    assert verdict.decision is Decision.ESCALATE


def test_forbidden_action_is_denied(make_request):
    verdict = _engine().evaluate(make_request("delete_reservation", reservation_id="r1"))
    assert verdict.decision is Decision.DENY


def test_liability_admission_escalates(make_request):
    """Section 3: empathy must not be delivered as an admission of fault."""
    bad = make_request(
        "send_message",
        body="I am so sorry, we failed to service the AC and it ruined your stay.",
    )
    assert _engine().evaluate(bad).decision is Decision.ESCALATE


def test_compliant_empathy_passes(make_request):
    """The approved phrasing from the identity document must not trip the rule."""
    good = make_request(
        "send_message",
        body=(
            "I understand how frustrating it is to arrive to a warm room after a long "
            "travel day. I am alerting our on-call technician and dispatching a "
            "portable cooling unit to your suite."
        ),
    )
    assert _engine().evaluate(good).decision is Decision.ALLOW


def test_most_restrictive_wins_and_all_reasons_are_reported():
    """A request tripping two rules yields the harsher verdict and both reasons."""
    engine = PolicyEngine(
        [
            ScopedPolicy(
                settings=PolicySettings(
                    forbidden_actions=frozenset({"send_message"}), max_message_length=10
                )
            )
        ]
    )
    request = ActionRequest(
        action="send_message",
        params={"body": "x" * 50},
        context=TenantContext(tenant_id="t1"),
    )
    verdict = engine.evaluate(request)

    assert verdict.decision is Decision.DENY
    assert len(verdict.reasons) == 2


def test_decision_combination_is_order_independent():
    assert Decision.most_restrictive([Decision.ALLOW, Decision.DENY]) is Decision.DENY
    assert Decision.most_restrictive([Decision.DENY, Decision.ALLOW]) is Decision.DENY
    assert Decision.most_restrictive([Decision.ESCALATE, Decision.ALLOW]) is Decision.ESCALATE
    assert Decision.most_restrictive([]) is Decision.ALLOW
