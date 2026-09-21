# TIMEO Concierge

Autonomous AI hospitality operations and concierge platform.
See [docs/PROJECT_IDENTITY.md](docs/PROJECT_IDENTITY.md) for the vision, philosophy
and governance model this implementation serves.

## Status

Phase 4 — durable approval queue, per-scope policy configuration, operator API.
117 tests. The domain core still has no runtime dependencies; `anthropic` is
needed only for live model calls and `fastapi` only for the operator API.

## The central invariant

Nothing may cause an external side effect except through `ActionGateway.dispatch`.

```
agent proposes action
   -> resolve_mode(tenant, property, channel)
   -> policy engine (deterministic)  -> ALLOW / ESCALATE / DENY
   -> execute | propose | escalate | deny
   -> audit (every path, including failures)
```

The three operating modes are a property of every side effect, not a feature to
add later. Adapters that call external systems directly would make SHADOW mode
impossible to implement without rewriting them, so the gateway comes first.

### Mode x decision matrix

|              | ALLOW            | ESCALATE            | DENY   |
| ------------ | ---------------- | ------------------- | ------ |
| `SIMULATION` | mock             | simulated task      | denied |
| `SHADOW`     | proposed         | proposed            | denied |
| `AUTONOMOUS` | **executed**     | approval task       | denied |

Only `AUTONOMOUS` + `ALLOW` reaches the outside world. Policy is evaluated in
`SIMULATION` too, so a simulated run rehearses the real decision path.

## Layout

```
src/timeo/
  domain/        entities, enums, errors — plain dataclasses, no ORM
  policy/        deterministic rules and evaluation
  gateway/       mode resolution, adapter registry, the action gateway
  audit/         append-only operational record
  repositories/  tenant-scoped storage contract + in-memory implementation
  agent/         system prompt, tool surface, context assembly, the loop
  evals/         scenario harness, deterministic checks, model-graded judge
  storage/       SQLite schema and stores (escalations, audit, policy)
  api/           operator API — approval queue, audit trail, policy config
tests/
```

## The agent loop

Written manually rather than with the SDK tool runner: every tool call becomes an
`ActionRequest` that the gateway adjudicates, and every turn leaves an audit entry
with its own cost. The loop proposes; it never executes.

What the model is told about an outcome differs by status, deliberately:

- **executed / simulated / proposed** — reads as success. In SHADOW that is the
  point: telling the agent the truth would change what it does next and make the
  transcript useless for calibration.
- **escalated** — reads as pending, so the agent says a manager is reviewing
  rather than promising an outcome nobody approved.
- **denied** — reads as an error with the rule attached, so the agent adapts
  instead of retrying a refused call.

The model's `tool_use` id is used as the gateway's dedupe key, so a replayed turn
sends once.

## Running

```bash
.venv/bin/pytest
```

Lint:

```bash
.venv/bin/ruff check src tests
```

## Design decisions worth knowing

- **Mode is resolved per event**, not read from an env var, so one property can
  run autonomously while another is still in shadow.
- **Shadow returns a synthetic tool result** so the agent loop keeps reasoning
  coherently about a message it believes it sent. Without this, shadow
  transcripts would diverge from live behaviour and be worthless for calibration.
- **Policy evaluates all rules**, not first-match, so rule order can never
  accidentally loosen policy and the audit trail shows everything that fired.
- **Cross-tenant reads raise** rather than return `None` — a breach must not be
  disguised as a cache miss.
- **The domain layer has no dependencies.** FastAPI and Postgres arrive as
  adapters; the core stays testable without either.

## Evals

```bash
.venv/bin/python -m timeo.evals          # deterministic checks only
.venv/bin/python -m timeo.evals --judge  # adds model-graded checks
```

Eight scenarios, each tracing to a claim the identity document makes — named in
the scenario's rationale, so a failure reads as "we stopped honouring X". Every
scenario runs in SIMULATION against mock adapters, so the suite is safe to run as
often as you like.

Expectations split in two. Deterministic checks read gateway outcomes and the
audit trail; they are exact, free, and run without an API key. Judged checks read
the reply text and need a model — the judge sees only the guest message and the
reply, never the policy verdict, since a judge that can see the system behaved
correctly will pass replies that did not.

## Operator API

```bash
.venv/bin/python -m timeo.api
```

Approval queue, audit trail, and per-scope policy configuration. **`X-Operator-Id`
is an identity header, not authentication** — it records who decided so the audit
trail names them; it does not verify them. Real auth and a tenant-membership
check are prerequisites for any non-local use.

Cross-tenant access returns 404 (never confirming another tenant's task exists)
while writing the attempt to the audit trail: opaque to the caller, loud in the
record.

## Persistence

SQLite, because the Postgres decision is still open and this machine has neither
Postgres nor Docker. What carries over and what does not:

- **Carries over:** the store interfaces, tenant scoping in every query, and the
  composition of tenant → property policy scopes.
- **Does not:** Postgres row-level security has no SQLite equivalent. Tenant
  isolation is currently enforced in application code and WHERE clauses only —
  that is the guard, not the guarantee. Restoring the second layer is part of the
  Postgres migration.
- **Already enforced by the database:** audit immutability, via triggers that
  abort any UPDATE or DELETE on `audit_entries`.

## Model configuration

`claude-opus-5` with adaptive thinking and `effort: high`. The system prompt is
frozen and sent as a single cacheable block; per-conversation context travels in
the messages so the cached prefix survives. Tool schemas are `strict` — policy
reads tool arguments, so an unvalidated refund amount would be a policy bypass.

## Not yet built

Channel adapters (Phase 5), Postgres with row-level security, real operator
authentication, and property RAG. The deterministic liability-phrase rule is
English-only — a decision deferred until the eval suite can show whether it earns
its keep at all.
