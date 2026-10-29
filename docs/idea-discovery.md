# Idea discovery and evidence refresh

Discovery screens the liquid members of the dated NSE constituent universe.
Completed-session prices, annual earnings, sector comparisons, exchange
announcements, earnings notices and delivery reports feed conditional plans.
Ranking does not approve orders or establish a profitable model.

`scripts/evidence_screen.py` repairs incompatible financial caches automatically.
It requests at most 400 statements per refresh, at most three concurrently,
with one second between submissions and an eight-minute submission budget.
Current controlled price structures get priority within each queue group;
untried symbols precede retries. Successful compatible histories leave the
queue, including honest short histories which still fail the quality rules.
Recent failures wait an hour. A 429/503 response stops new submissions while
already running responses are drained and archived. A later scheduled cycle
resumes pending coverage. Each source capture retains its actual availability
time; original evidence and publications are immutable.

The regular 30-minute timer uses this bounded catch-up without a manual daily
backfill. The command writes only the dedicated screening and membership
stores; it never imports the broker or strategy startup. A smaller refresh
can be requested with `--fundamentals-limit 40`; zero skips financial requests.

The Ideas API adds `stock_plans.discovery_health`: current screened count,
dated earnings-history coverage, feed errors and counted rejection reasons.
Counts overlap when one stock fails several independent checks. The empty
state distinguishes incomplete/stale evidence from fully checked rejections.
History coverage does not imply three profitable years or complete bank
capital/asset-quality evidence. Those requirements remain explicit predicates.

Research discovery does not apply the execution regime. Qualifying conditional
plans can be visible in OFF; orders still require the existing market gate,
entry confirmation, dated instrument rules and account-specific risk checks.
No daily quota is introduced. Stops, targets, sizing, the zero-to-three cap,
sector limit and the registered forward experiment remain unchanged.
