---
name: chicas-lindas-dual-repo-release
description: Prepare and verify coordinated releases across the independent Barcelona barcebot, Madrid replaybot, and billing-control-plane repositories while preserving persistent sessions, testing all six bots, and enforcing reversible PMBOK release gates.
---

# Dual-repository release

Use this skill for coordinated billing changes, releases, canaries, rollbacks, or parity checks across the three repositories.

## Repository boundaries

- `barcebot`: Barcelona, base branch `main`.
- `replaybot`: Madrid, base branch `master`.
- `billing-control-plane`: independent service, base branch `main`.
- Work in `feat/monthly-billing-entitlements` in every repository.

Read [references/repository-matrix.md](references/repository-matrix.md) before editing shared behavior. Never copy an entire `app.py`, entrypoint, worker definition, or deployment file from one bot repository to the other. Copy only deliberately identical billing clients, tests, documentation, and skills after comparing their surrounding integrations.

## Release gates

1. Commercial and fiscal approvals recorded.
2. Threat model and security diff review complete.
3. Provider sandbox scenarios pass.
4. UAT covers all three channels in each city plus panels and offline cache.
5. Shadow mode demonstrates no false blocks.
6. Explicitly authorized one-city canary succeeds.
7. Explicitly authorized rollout to all six bots succeeds.

Read [references/acceptance.md](references/acceptance.md) for acceptance evidence. Remote deploys, restarts, secret rotation, or enabling enforcement always require explicit authorization.

## Persistence and rollback

Treat `/app/data`, WhatsApp auth, Telegram sessions, QR state, databases, and configurations as protected assets. Billing suspension must not delete or mutate them. The reversible rollback is `BILLING_ENFORCEMENT=false`; do not roll back by erasing financial events or sessions.

## Validation

Run `scripts/check_dual_repo_parity.py --workspace-root <workspace>`, then execute the full Python and Node suites in both bot repositories and the control-plane suite.
