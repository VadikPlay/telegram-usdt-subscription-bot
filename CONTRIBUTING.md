# Contributing

Bug reports and patches are welcome. This project moves money and controls access
to people's paid communities, so the bar for changes is deliberately high.

## Before opening a PR

```bash
pip install -e ".[dev]"
pytest -q                     # must pass
python scripts/selftest.py    # must pass
ruff check src tests scripts  # must be clean
```

## Rules for anything that touches money or access

1. **Keep the core pure.** `services/matcher.py` and `services/access_rules.py`
   take arguments and return decisions — no database, no network, no `now()`
   inside them. Do not "just quickly" add a query there.
2. **No floats, ever.** Amounts are `Decimal` end to end. A single `float` in the
   path is a bug even if the tests pass.
3. **New behaviour needs a test that would fail without it.** If you fix a
   mis-credit, add the case that mis-credited.
4. **Never loosen exact amount matching.** Tolerant matching is how the wrong
   person gets access. If a real payment is being rejected, fix the invoice
   allocation or the time window, not the equality check.
5. **Removal stays conservative.** If you add a path that can remove a member,
   it goes through `access_rules.decide` and respects `SAFE_MODE`, the grace
   period and payment protection.
6. **Audit everything.** Any new access action writes an `AuditLog` row.

## Reporting a security issue

Do not open a public issue for anything that could let someone obtain access
without paying, or read another subscriber's data. Contact the maintainer
privately first.

## Reporting a bug

Include: what you expected, what happened, the relevant log lines with tokens and
wallet addresses redacted, your `/config` output (also redacted), and whether
`python scripts/selftest.py` passes on your machine.
