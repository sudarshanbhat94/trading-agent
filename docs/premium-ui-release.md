# Premium UI release preparation — 27 October 2026

User authorization covers deploying the approved premium design to OCI. The UI
source is commit `7c5e1a1`; production was observed at `d8dcb7a` before cutover.
This document records preparation, not a deployment-success receipt.

Both exact-UI-commit Python 3.12 and 3.14 CI jobs passed. The dependency job found
two aliases of one newly reported issue in the previous `certifi==2026.5.20`
trust bundle: [GHSA-6q48-6c7w-9gw2](https://osv.dev/vulnerability/GHSA-6q48-6c7w-9gw2).
The advisory is fixed from 2026.10.18. The release pins
[`certifi==2026.10.23`](https://pypi.org/project/certifi/2026.10.23/) with official
distribution hashes, updates its lock/SBOM records, and sets a safe manifest floor.
All other package versions remain unchanged. The updated live OSV check reports
39 packages checked and zero known active advisories; this is not independent
security certification.

Cutover must preserve account/book epochs, original financial rows, settings,
broker-state files and the registered forward protocol. The local rollout uses
an encrypted, authenticated backup, an escrowed local key, scoped certificate-wheel
installation, previous-code/package rollback on failure, and restoration of the
original running services/timers. No strategy parameter, model permission, broker
setting, portfolio reset or real order is part of this release.

Production verification must establish the served premium CSS/JavaScript, local
font hashes, service health and actual aggregate paper state. Browser session
availability limits must be stated explicitly. Private deployment receipts remain
outside the public repository.
