# Veto

Veto is a work-in-progress policy verifier and runtime guard for agentic
payments. An agent may propose a payment, but Veto will check the proposed
action against a mandate before any payment API is called.

## What we are building

```text
mandate text -> parser -> policy checks -> enforceable rules
                                              |
agent proposal -> structured action -> runtime guard -> payout adapter
                                      |
                                      +-------> audit log
```

The first end-to-end demo will show a payment above a mandate's cap being
rejected, a compliant payment being approved, and both decisions recorded in
an audit trail. It will use simulated payouts so it requires no money or API
keys. RazorpayX test mode integration is planned for a later stage.

## Current state

The Python package, mandate grammar, example policies, parser, typed models,
policy loader, both tiers of static checks, and VetoGate's policy checks are
in place. VetoGate evaluates merchant rules, spending caps, validity windows,
preconditions, and action order using confirmed action history. A SQLite store
now preserves that history across plans and restarts. A separate hash-chained
audit log is also in place. The payment pipeline connects the guard, a
pluggable executor, confirmed history, and audit records. A payout adapter and
demo commands are still to come.

## Mandate syntax

```text
mandate VendorPayments {
  rule R1 {
    cap payout <= 50000 INR per 24h
    allow_merchant "vendor_123"
    valid_window 2026-01-01T00:00:00Z to 2035-01-01T00:00:00Z
  }
  rule R2 {
    require invoice_match before payout
    sequence [invoice_match, payout]
  }
}
```

The grammar also supports `deny_merchant`, `payment` and `refund` actions,
`USD` caps, and window units `s`, `m`, `h`, `d`, and `w`. Timestamps must carry
an explicit UTC offset. See [the grammar](src/veto/mandate.lark) for the exact
syntax.

## Example mandates

[Vendor payments](examples/vendor_payments.mdt) is the basic policy;
[strict treasury](examples/strict_treasury.mdt) adds a validity window and
action order for the planned demo. The files prefixed `flawed_` in
[examples/](examples/) each show a specific mistake. The bare timestamp file
is a syntax error; the precondition cycle and sequence conflict are found by
Z3. The other flawed examples produce fast static diagnostics.

To parse a mandate after installation:

```bash
python -c 'from veto.mandate_parser import parse_mandate_file; print(parse_mandate_file("examples/vendor_payments.mdt"))'
```

Syntax errors include the file, line, column, and a pointer to the problem.
Parsing alone does not decide whether the mandate's rules are valid.

To load a typed policy:

```bash
python -c 'from veto.policy_loader import load_policy_file; print(load_policy_file("examples/strict_treasury.mdt"))'
```

The loader rejects invalid calendar dates, fractions of a cent, and time
windows that cannot be represented as whole seconds. It keeps potential
conflicts such as duplicate rule ids and inverted validity windows for the
static checks to diagnose.

The [static checks](src/veto/policy_checks.py) now report duplicate rule ids,
merchant allow/deny conflicts and disjoint allowlists during overlapping
validity windows, inverted windows, repeated or shadowed caps with the same
scope, self-preconditions, and repeated sequence steps. All rules apply
together, so their merchant allowlists intersect. A zero cap produces a
warning. To inspect one:

```bash
python -c 'from veto.policy_loader import load_policy_file; from veto.policy_checks import check_policy; print(*check_policy(load_policy_file("examples/flawed_shadowed_caps.mdt")), sep="\n")'
```

The optional [Z3 check](src/veto/z3_checks.py) combines preconditions and
sequences from rules that are valid at the same time. It reports action-order
cycles that prevent completing the involved actions; it does not claim that
every action in the mandate is impossible. Run both tiers with:

```bash
python -c 'from veto.policy_loader import load_policy_file; from veto.z3_checks import verify_policy; print(*verify_policy(load_policy_file("examples/flawed_precondition_cycle.mdt")), sep="\n")'
```

Install the `smt` extra for this check. Without Z3, policies with action-order
rules receive a `z3_unavailable` warning instead of silently skipping it.

## VetoGate

The [runtime gate](src/veto/vetogate.py) checks merchant allowlists and
denylists, per-action and rolling-window caps, validity windows, historical
preconditions, and sequence order. A money-moving action without a matching
cap is rejected. The gate requires a trusted current time and a complete list
of confirmed earlier actions. It returns a verdict but does not execute a
payout or record an approved action yet.

```python
from datetime import datetime, timezone
from veto.models import PaymentAction
from veto.policy_loader import load_policy_file
from veto.vetogate import VetoGate

gate = VetoGate(load_policy_file("examples/basic_payouts.mdt"))
action = PaymentAction(type="payout", amount="40.00", currency="INR", merchant="vendor_123")
verdict = gate.evaluate(action, now=datetime.now(timezone.utc), executed=())
print(verdict)
```

Prior actions satisfy preconditions and sequence steps only for the same
merchant and currency. A sequence governs its first ordered pass; after that,
other policy checks continue to apply. The current action model has no invoice
identifier, so `invoice_match` is not yet tied to one specific payout. That
binding must be added before using this as a real invoice-payment control.

## Confirmed action history

The [SQLite history store](src/veto/state.py) keeps only confirmed actions.
Pass `store.confirmed()` to VetoGate for each proposal. Once an external
executor confirms success, call `store.record_confirmed(event_id, executed)`
with its stable event id and an `ExecutedAction` stamped by trusted code.
Repeating the same event id and content is idempotent; reusing the id for a
different action fails.

Approval alone must not be recorded as execution. The pipeline records an
action here only after its executor returns a confirmed receipt.

## Audit trail

The [audit log](src/veto/audit.py) stores approved, rejected, and execution
error decisions in a separate SQLite table. Each entry includes the action,
reason, time, and a fingerprint of the policy used. Its hash covers the
previous hash and canonical entry content. `verify_chain()` reports the first
broken sequence number, and appending to a broken chain fails. The history
and audit tables can share one database file.

This is tamper-evident against local edits or deletion of entries. Someone
who can rewrite the whole database and recompute the hashes could forge a new
chain; external anchoring or signatures would be needed to resist that.
The pipeline writes its decisions to this log. A confirmed action and its
approved audit entry commit in the same local SQLite transaction.

## Payment pipeline

The [pipeline](src/veto/pipeline.py) checks a proposal against confirmed
history, calls a supplied executor only for approved proposals, then records
the result. The executor receives a stable idempotency key derived from the
policy and event id. Replaying an already recorded event returns its original
result without calling the executor again. Rejections and definite execution
failures receive audit entries but never enter confirmed history.

The current tests use a fake executor. No payment provider or credentials are
needed for this checkpoint. The executor must honor the idempotency key and
return a receipt only when execution is confirmed. A timeout, queued receipt,
or other uncertain outcome raises `ExecutionUncertainError` and does not enter
confirmed history. Resolve that event with the provider before making another
payment; retrying it requires the same event id and an idempotent executor.

The local database transaction serializes pipeline calls and commits confirmed
history with the audit record. It cannot roll back an external payout. If the
process stops after a provider succeeds but before the local commit, Veto may
have no record of that payout. The pipeline therefore does not yet provide
safe live-money recovery or multi-worker operation. A durable pending state,
provider reconciliation, and invoice-to-payout binding are still needed.

## Policy and action data

The [validated models](src/veto/models.py) represent spending caps, policy
rules, and proposed actions. Money amounts use decimal values and reject
fractions of a cent. Policy dates require a timezone offset. An agent's action
does not carry a timestamp; the runtime guard uses a trusted clock when it
checks validity windows.

## Set up the development environment

Use Python 3.11 or newer. From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,smt,server]'
python -c 'import veto; print(veto.__file__)'
```

The `smt` extra provides Z3 for policy checks, `server` provides the planned
API, and `dev` provides pytest. These extras are installed during development;
the core package can also be installed with `python -m pip install -e .`.

Run the current test suite with `python -m pytest -q`. GitHub Actions runs it
on pushes and pull requests using Python 3.11 and 3.14. The workflow installs
the core package with the `dev` and `smt` extras; later stages will extend it
when other optional components need their own tests.

## Planned boundaries

- The agent proposes actions; it does not decide whether they satisfy policy.
- The runtime guard checks each action before the payout adapter runs.
- The default demo uses simulated payouts. RazorpayX test mode will use test
  credentials; live payouts are outside the demo scope.
- Decisions and execution results will be recorded for inspection.

## License

MIT. See [LICENSE](LICENSE).
