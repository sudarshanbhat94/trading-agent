# Individual-stock coverage and index entry status

Presentation/API correction; no trading-rule or permission change, no deployment.

The home page described the NIFTYBEES completed-session trend as the entire strategy. Meanwhile the official constituent evidence job screened individual stocks independently, and the paper orchestrator allowed only `index_directional`. Its `quality_momentum` observations were stripped before allocation. Screening coverage and automated execution were therefore different capabilities, which the main status card failed to distinguish.

The overview now includes additive `stock_screen` and `execution_scope` objects. The stock summary reads the same completed-session evidence source used by Ideas: actual distinct stock coverage, constituent count, data date, preliminary evidence passes and leading rejection reasons. It does not create a publication, plan, account reservation or order. Preliminary passes still need entry levels, after-cost economics, account risk and approval. Unknown coverage is not zero qualifying stocks. Stale counts are historical and do not expose current pass counts; malformed/duplicate evidence cannot increase passes.

The home renderer gives individual-stock screening its own panel ahead of a separate index-entry panel. It displays the actual configured stock-execution restriction. An OFF index state no longer becomes a claim that only an ETF is screened, and an ON state cannot imply stock automation is enabled. Existing positions are not described as all cash. External rejection text is escaped.

This repair does not enable the requested individual-stock automation. The hard production allowlist remains unchanged. Independent stock-model evidence and the owned execution lifecycle must be completed before model promotion. Pushing this review branch does not change the running website.

Validation: 23 focused coverage/API/real-JavaScript/integrity checks passed. The final isolated full suite ran 2,589 tests: 2,456 passed, 133 skipped, zero failures/errors. All 103 research/UI function fixtures passed. The suite used disposable databases, disabled dotenv loading and blocked external sockets, allowing localhost mock servers. Actual overview handling was checked with an isolated paper schema; actual home JavaScript was executed for OFF/ON, held positions, missing/stale coverage and hostile rejection text. These are API/renderer checks, not a production browser or trading certification.
