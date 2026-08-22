# Acceptance evidence

## Automated

- Control-plane Python tests pass.
- Barcelona Python and Node tests pass.
- Madrid Python and Node tests pass.
- Skill validation and deterministic contract/parity scripts pass.
- `git diff --check`, secret scan, and security diff review pass in each repository.

## Sandbox and UAT

- Stripe and PayPal cover payment, renewal, failure, recovery, refund, chargeback and cancellation. PSE/APM and crypto cover order correlation, wrong local/contract amount, pending/final state, expiry, replay and reversal. Every enabled dLocal method code has its own sandbox evidence.
- WhatsApp, Telegram UserBot, and BotFather in both cities stop before replies and state changes when denied.
- Panels remain available, session artifacts remain unchanged, and a verified payment reactivates within two minutes without restart or QR.
- Six-hour signed-cache outage behavior and fail-closed expiry are demonstrated.

## Promotion

Record gate owner, evidence link, outcome, timestamp, and rollback decision. Calendar entries may remind humans about gates but never change billing state.
