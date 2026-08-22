---
name: chicas-lindas-billing-entitlements
description: Implement or review the account-level monthly billing entitlement for the six Madrid and Barcelona bots, including Stripe, PayPal, PSE/APMs, crypto, provider approval gates, signed offline cache, privacy, suspension, reactivation, and audited overrides.
---

# Billing entitlements

Use this skill for any change to payment state, access enforcement, the billing panel, provider webhooks, or deployment billing credentials.

## Fixed contract

- Treat all six bots as one account and one EUR 250 monthly subscription.
- Keep every provider disabled until its written commercial approval and fiscal approval exist.
- Grant seven trial days only to a genuinely new account; never restart the current client's trial.
- Allow 72 hours after a failed or expired payment. Suspend immediately after a refund or chargeback.
- Let cancellation run only to the already-paid period end.
- Limit a manual override to 24 hours and require actor, reason, timestamps, and audit entry.
- Never store cards, message content, phone numbers, JIDs, provider payloads, or dLocal payer identity fields.

Read [references/contract.md](references/contract.md) before changing prices, states, or tax behavior. Read [references/state-machine.md](references/state-machine.md) before changing webhook or entitlement logic. Read [references/operations.md](references/operations.md) before rollout or incident work.

## Required controls

1. Authenticate Stripe, PayPal, dLocal and BitPay webhooks using the provider's official verification mechanism and original event.
2. Require a unique provider event ID and process duplicates idempotently.
3. Verify recurrent payments against the configured EUR 250 plan. For PSE/APM/crypto, require a control-plane payment order, matching provider IDs, exact contractual/local amounts and final paid state.
4. Treat events as unordered. A stale failure/cancellation cannot undo a newer payment, while refunds and chargebacks always suspend.
5. Sign deployment-scoped entitlements with Ed25519. Keep the private key only in the control plane.
6. Refresh each deployment every 60 seconds. Permit a verified cache for at most six hours, then fail closed only when enforcement is enabled.
7. Run the gate before any reply or conversational-state mutation. Keep panels and session files intact.
8. Keep `BILLING_ENFORCEMENT=false` until shadow telemetry, UAT, and canary gates pass.
9. Expose only methods that are both approved and fully configured. Test every dLocal method code separately in sandbox.

## Validation

Run `scripts/verify_entitlement_contract.py --workspace-root <workspace>` and the repository test suites. Do not deploy, restart, create a live price, or enable enforcement without explicit authorization.
