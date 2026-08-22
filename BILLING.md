# Billing entitlements

This deployment consumes a signed account-level entitlement from the shared billing control plane. It never receives Stripe, PayPal, dLocal or BitPay secrets, nor the Ed25519 private key.

## Deployment environment

Set these values in Easypanel, not in committed files:

- BILLING_CONTROL_PLANE_URL: HTTPS service URL.
- BILLING_DEPLOYMENT_ID: barcelona in barcebot or madrid in replaybot.
- BILLING_DEPLOYMENT_TOKEN: individual opaque token of at least 32 characters.
- BILLING_SIGNING_PUBLIC_KEYS_JSON: JSON map from key_id to base64-encoded Ed25519 public PEM.
- BILLING_ACCOUNT_ID=chicas-lindas.
- BILLING_REFRESH_SECONDS=60.
- BILLING_TIMEOUT_SECONDS=3.
- BILLING_ENFORCEMENT=false during local, sandbox, shadow, and initial canary work.
- BILLING_ALLOW_INSECURE_HTTP=false; enable only for an explicitly approved private service network.

Only the panel process may receive BILLING_CONTROL_PLANE_ADMIN_TOKEN. The entrypoint strips it from WhatsApp, Telegram UserBot, and BotFather workers.

## Runtime behavior

Every channel checks the local verified entitlement before responding or mutating conversational state. The signed cache lives under /app/data and is valid for at most six hours. Suspension leaves panel access, WhatsApp auth, Telegram sessions, QR data, configuration, and other persistent files untouched.

The authenticated panel renders only the payment methods returned by the control plane. Stripe/PayPal are recurring; PSE/APM and crypto use a correlated monthly order. Payer fields required by dLocal are forwarded for checkout and are not written to this repository's data, audit or metrics.

## Rollout and rollback

Promote in this order: approvals, sandbox, shadow, UAT, one-city canary, all six bots. Remote deployment, restart, secret rotation, or enabling enforcement requires explicit authorization. The reversible rollback is BILLING_ENFORCEMENT=false; do not delete sessions or billing history.
