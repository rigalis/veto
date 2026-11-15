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

The Python package, mandate grammar, example policies, and parser are in
place. The parser produces structured rules with source locations. Value and
conflict checks, the runtime guard, payment adapter, and demo commands are
still to come. Each part will be added and checked in a separate development
checkpoint.

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
is a syntax error now; the other mistakes will be diagnosed by later policy
checks.

To parse a mandate after installation:

```bash
python -c 'from veto.mandate_parser import parse_mandate_file; print(parse_mandate_file("examples/vendor_payments.mdt"))'
```

Syntax errors include the file, line, column, and a pointer to the problem.
Parsing alone does not decide whether the mandate's rules are valid.

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

## Planned boundaries

- The agent proposes actions; it does not decide whether they satisfy policy.
- The runtime guard checks each action before the payout adapter runs.
- The default demo uses simulated payouts. RazorpayX test mode will use test
  credentials; live payouts are outside the demo scope.
- Decisions and execution results will be recorded for inspection.

## License

MIT. See [LICENSE](LICENSE).
