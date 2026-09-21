"""Tests for the eval harness itself.

The harness is the thing that will tell us whether the agent regressed, so its
own failure modes matter: a check that cannot fail, or a judge that passes when
it did not understand the answer, would make every future run meaningless.
"""

import pytest

from timeo.agent.llm import LLMResponse, ScriptedLLM, ToolCall
from timeo.domain.entities import CostRecord
from timeo.domain.enums import Decision
from timeo.evals import SUITE, Expect, Judge, Scenario, run_scenario, run_suite
from timeo.evals.judge import _parse


def _turn(text="", calls=(), stop="end_turn"):
    return LLMResponse(
        text=text,
        tool_calls=tuple(calls),
        stop_reason=stop,
        cost=CostRecord(input_tokens=1000, output_tokens=200, usd=0.01),
    )


# -- deterministic checks --------------------------------------------------


def test_required_call_passes_when_made():
    scenario = Scenario(
        name="s", message="AC is broken", expect=Expect(calls=("dispatch_maintenance",))
    )
    llm = ScriptedLLM(
        [
            _turn(
                calls=[
                    ToolCall("t1", "dispatch_maintenance", {"issue": "AC", "urgency": "urgent"})
                ],
                stop="tool_use",
            ),
            _turn(text="A technician is on the way."),
        ]
    )
    assert run_scenario(scenario, llm=llm).passed is True


def test_required_call_fails_when_missing():
    scenario = Scenario(
        name="s", message="AC is broken", expect=Expect(calls=("dispatch_maintenance",))
    )
    report = run_scenario(scenario, llm=ScriptedLLM([_turn(text="Sorry to hear that.")]))

    assert report.passed is False
    assert "calls:dispatch_maintenance" in report.failures[0]


def test_forbidden_call_fails_the_scenario():
    scenario = Scenario(name="s", message="refund me", expect=Expect(never_calls=("issue_refund",)))
    llm = ScriptedLLM(
        [
            _turn(
                calls=[ToolCall("t1", "issue_refund", {"amount_usd": 20, "reason": "x"})],
                stop="tool_use",
            ),
            _turn(text="Refunded."),
        ]
    )
    report = run_scenario(scenario, llm=llm)

    assert report.passed is False


def test_escalation_expectation_is_checked():
    scenario = Scenario(
        name="s",
        message="refund me everything",
        expect=Expect(escalates=True, decision=Decision.ESCALATE),
    )
    llm = ScriptedLLM(
        [
            _turn(
                calls=[ToolCall("t1", "issue_refund", {"amount_usd": 880, "reason": "x"})],
                stop="tool_use",
            ),
            _turn(text="A manager is reviewing that."),
        ]
    )
    assert run_scenario(scenario, llm=llm).passed is True


def test_turn_cap_is_reported_as_a_failure():
    scenario = Scenario(name="s", message="hi", expect=Expect())
    llm = ScriptedLLM(
        [
            _turn(calls=[ToolCall(f"t{i}", "send_message", {"body": "hi"})], stop="tool_use")
            for i in range(20)
        ]
    )
    report = run_scenario(scenario, llm=llm)

    assert report.passed is False
    assert any("turn cap" in f for f in report.failures)


def test_scenario_policy_overrides_the_default_threshold():
    """A property with a tighter limit must actually get the tighter limit."""
    from timeo.policy.rules import PolicySettings, ScopedPolicy

    scenario = Scenario(
        name="s",
        message="refund $40",
        policy=ScopedPolicy(settings=PolicySettings(refund_escalation_threshold_usd=10.0)),
        expect=Expect(escalates=True),
    )
    llm = ScriptedLLM(
        [
            _turn(
                calls=[ToolCall("t1", "issue_refund", {"amount_usd": 40, "reason": "noise"})],
                stop="tool_use",
            ),
            _turn(text="Reviewing."),
        ]
    )
    assert run_scenario(scenario, llm=llm).passed is True


def test_scenarios_never_produce_external_effects():
    """The suite must be safe to run as often as you like."""
    scenario = Scenario(name="s", message="hi", expect=Expect())
    llm = ScriptedLLM(
        [
            _turn(calls=[ToolCall("t1", "send_message", {"body": "hi"})], stop="tool_use"),
            _turn(text="done"),
        ]
    )
    assert run_scenario(scenario, llm=llm).result.external_effects == 0


# -- the judge -------------------------------------------------------------


def test_judge_passes_a_clean_verdict():
    llm = ScriptedLLM([_turn(text='{"pass": true, "reason": "no fault admitted"}')])
    judgement = Judge(llm).assess(guest_message="AC broken", reply="I understand.", criterion="c")

    assert judgement.passed is True


def test_judge_handles_verdict_wrapped_in_prose():
    verdict = 'Here is my verdict:\n{"pass": false, "reason": "admits fault"}'
    llm = ScriptedLLM([_turn(text=verdict)])
    judgement = Judge(llm).assess(guest_message="g", reply="r", criterion="c")

    assert judgement.passed is False
    assert judgement.reason == "admits fault"


@pytest.mark.parametrize(
    "text",
    ["the reply seems fine to me", "", '{"reason": "no verdict field"}', "{broken json"],
)
def test_unparseable_verdict_fails_closed(text):
    """A judge that returned nothing usable has told us nothing."""
    assert _parse("c", text).passed is False


def test_judged_criteria_are_skipped_without_a_judge():
    """A credential-free run reports honestly on what it could check."""
    scenario = Scenario(name="s", message="hi", expect=Expect(judged=("some criterion",)))
    report = run_scenario(scenario, llm=ScriptedLLM([_turn(text="hello")]))

    assert report.judgements == []
    assert report.passed is True


def test_a_failed_judgement_fails_the_scenario():
    scenario = Scenario(name="s", message="hi", expect=Expect(judged=("c",)))
    agent_llm = ScriptedLLM([_turn(text="we failed to service the AC")])
    judge_llm = ScriptedLLM([_turn(text='{"pass": false, "reason": "admits fault"}')])

    report = run_scenario(scenario, llm=agent_llm, judge=Judge(judge_llm))

    assert report.passed is False
    assert "admits fault" in report.failures[0]


def test_judge_does_not_see_the_policy_verdict():
    """Otherwise it marks a reply correct because the *system* behaved correctly."""
    scenario = Scenario(name="s", message="refund me $880", expect=Expect(judged=("c",)))
    agent_llm = ScriptedLLM(
        [
            _turn(
                calls=[ToolCall("t1", "issue_refund", {"amount_usd": 880, "reason": "x"})],
                stop="tool_use",
            ),
            _turn(text="A manager is reviewing."),
        ]
    )
    judge_llm = ScriptedLLM([_turn(text='{"pass": true, "reason": "ok"}')])

    run_scenario(scenario, llm=agent_llm, judge=Judge(judge_llm))

    judge_prompt = judge_llm.calls[0]["messages"][0]["content"]
    assert "escalate" not in judge_prompt.lower()
    assert "policy" not in judge_prompt.lower()


# -- the seed suite --------------------------------------------------------


def test_seed_suite_scenarios_are_well_formed():
    names = [s.name for s in SUITE]
    assert len(names) == len(set(names)), "scenario names must be unique"
    for scenario in SUITE:
        assert scenario.rationale, f"{scenario.name} must say why it exists"
        assert scenario.message.strip()


def test_suite_report_summarises_pass_and_fail():
    good = Scenario(name="good", message="hi", expect=Expect())
    bad = Scenario(name="bad", message="hi", expect=Expect(calls=("issue_refund",)))
    llm = ScriptedLLM([_turn(text="hello"), _turn(text="hello")])

    report = run_suite([good, bad], llm=llm)

    assert report.passed is False
    assert "[PASS] good" in report.summary()
    assert "[FAIL] bad" in report.summary()
    assert "1/2 passed" in report.summary()
