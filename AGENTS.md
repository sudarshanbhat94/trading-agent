# OpenStocks workflow

- Read `HANDOFF.md` once for orientation, then verify relevant facts in current code/data. Do not treat dated notes as live state.
- Search only relevant paths with `rg`; cap tool output and avoid dumping whole files, logs, or test runs into context.
- Finish one requested milestone at a time. Run focused checks first and the full suite once when the change warrants it; do not repeat passing checks without a reason.
- Keep final reports to 1–5 short lines: outcome, evidence, and any material limit. Do not claim profitability or live readiness without proof.
- Protect paper-book correctness and public-repo secrets. If trading behaviour changes, mark it prominently in the commit message and final report.
