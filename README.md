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
policy loader, both tiers of static checks, and the first VetoGate checks are
in place. VetoGate currently evaluates merchant rules and spending caps using
confirmed action history. Validity windows, action order, state persistence,
the payment adapter, and demo commands are still to come. Each part will be
added and checked in a separate development checkpoint.

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

## VetoGate so far

The [runtime gate](src/veto/vetogate.py) checks merchant allowlists and
denylists, per-action caps, and rolling-window caps. A money-moving action
without a matching cap is rejected. The gate requires a trusted current time
and a complete list of confirmed earlier actions. It returns a verdict but
does not execute a payout or record an approved action yet.

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

Policies containing validity windows, preconditions, or sequences currently
receive an explicit rejection from the gate. Their runtime checks are the
next checkpoint; they are never silently treated as satisfied.

## Policy and action data

The [validated models](src/veto/models.py) represent spending caps, policy
rules, and proposed actions. Money amounts use decimal values and reject
fractions of a cent. Policy dates require a timezone offset. An agent's action
does not carry a timestamp; the future runtime guard will use a trusted clock
when it checks validity windows.

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
