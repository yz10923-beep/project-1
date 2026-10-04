# Migrating off riskkit.legacy

riskkit 2.0 removes `riskkit/legacy.py`. Every caller moves to `riskkit.measures`:

| old | new |
|---|---|
| `legacy_var(returns, conf)` | `VaR(conf).of(returns)` |
| `legacy_es(returns, conf)` | `ExpectedShortfall(conf).of(returns)` |

Read the conventions in `riskkit/measures.py` before you change a caller: results seen
by users of this package must not change.

Checklist:

- [ ] every caller in riskkit/ uses riskkit.measures
- [ ] riskkit/legacy.py deleted, and nothing imports it
- [ ] CHANGELOG.md updated under Unreleased
