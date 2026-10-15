# Gemini handoff: OpenStocks + OCI

This repository is public. Do not paste, print, commit, log, or transmit private keys, access tokens, passwords, host addresses, or broker credentials. Keep all values below in Gemini's local secret/environment configuration.

## Local secret references

Configure these locally before Gemini uses SSH:

```bash
export OCI_HOST='set-in-local-secret-store'
export OCI_USER='set-in-local-secret-store'
export OCI_SSH_KEY_PATH='set-an-absolute-path-to-a-local-private-key'
export OCI_SSH_PORT='22'
```

The private key remains only on the local machine. `OCI_HOST` must never be added to source, reports, screenshots, commits, shell history, or issue text.

## Connection check

```bash
ssh -i "$OCI_SSH_KEY_PATH" -o IdentitiesOnly=yes -o BatchMode=yes -p "$OCI_SSH_PORT" "$OCI_USER@$OCI_HOST" 'hostname; cd /opt/opentrade && git status --short && git log -1 --oneline'
```

Only run read-only inspection first. Do not copy a private key, environment file, database, broker-state file, recovery key, or token off the server.

## Deployment facts

- Server checkout: `/opt/opentrade`
- Python environment: `/opt/opentrade/.venv`
- Application service: `opentrade`
- Paper and market data may be in `/opt/opentrade/var/`; treat every database and state file as private.
- The public Git branch currently in use is `codex/release-hardening-2026-10-07`.
- Upstox state is account-bound and per-user. Never read, display, copy, or change another user's broker state.

## Pasteable Gemini prompt

```text
You are maintaining OpenStocks at /Users/pavithramayya/Documents/Sudarshan/trading-agent. The repository is public.

Security rules:
- Never reveal, print, commit, paste, or upload OCI host values, SSH private-key contents, .env files, access tokens, cookies, passwords, broker state, database contents, or user identifiers.
- Use only local secret references: OCI_HOST, OCI_USER, OCI_SSH_KEY_PATH, OCI_SSH_PORT. Do not ask me to paste their values into chat.
- Never make a real broker order, modify broker credentials, reset a portfolio, or deploy unless I explicitly request that exact action.

OCI connection:
ssh -i "$OCI_SSH_KEY_PATH" -o IdentitiesOnly=yes -o BatchMode=yes -p "$OCI_SSH_PORT" "$OCI_USER@$OCI_HOST"

Deployed app:
- checkout: /opt/opentrade
- venv: /opt/opentrade/.venv
- systemd service: opentrade
- mutable data is private under /opt/opentrade/var/

Start every server investigation read-only:
1. Confirm the checked-out commit, git status, service health and recent non-sensitive logs.
2. Confirm that current paper-book figures reconcile without printing personal data.
3. Confirm quote freshness, scheduler health, idea publication/track counts and execution health using aggregate values only.
4. Compare the deployed commit with the requested branch. State exactly what is deployed versus merely committed.

For code changes:
1. Read AGENTS.md first; book correctness and secret hygiene outrank feature delivery.
2. Change one bounded concern at a time and preserve existing user changes.
3. Run focused tests, tests/test_repo_secret_hygiene.py, and the relevant full check before any push.
4. If a change alters trading behaviour, say so prominently in the commit message and report.
5. Never claim profitability, broker readiness or live certification without independent evidence.

For deployment, only after I explicitly authorise it:
1. Fetch the named commit in /opt/opentrade without exposing local configuration.
2. Run the specified migrations/checks against a recoverable backup and verify no private data is emitted.
3. Restart only the opentrade service.
4. Verify the service, authenticated UI and aggregate book state. Do not force a trade or turn a gate ON just to create activity.
5. Report the deployed commit, tests run and any remaining limitation in five short lines or fewer.
```

## Safe operational commands

```bash
# Read-only service and code inspection
ssh -i "$OCI_SSH_KEY_PATH" -o IdentitiesOnly=yes -p "$OCI_SSH_PORT" "$OCI_USER@$OCI_HOST" \
  'cd /opt/opentrade && git status --short && git log -1 --oneline && systemctl is-active opentrade'

# Follow non-sensitive service output briefly; do not copy raw logs into public artifacts.
ssh -i "$OCI_SSH_KEY_PATH" -o IdentitiesOnly=yes -p "$OCI_SSH_PORT" "$OCI_USER@$OCI_HOST" \
  'journalctl -u opentrade --since "15 minutes ago" --no-pager | tail -n 120'
```

Do not add a command that prints an environment file, private key, database dump, broker JSON, token, or credential-bearing process environment.
