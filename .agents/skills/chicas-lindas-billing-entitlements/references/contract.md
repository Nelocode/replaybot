# Commercial and data contract

## Commercial constants

- Account scope: Madrid and Barcelona deployments, six bots total.
- Price: EUR 250.00 every month.
- Trial: seven days for a new account only.
- Grace: 72 hours.
- Manual override: at most 24 hours.
- Taxes: not automated in version 1; fiscal invoicing remains an external approved process.

The historical EUR 200 charge is out of scope. Hosted provider surfaces are the only card/crypto-facing surfaces. PSE and other Colombian APMs settle a control-plane order denominated contractually in EUR and shown locally in COP.

## Approval gate

Production activation requires evidence of written approval from each enabled provider, fiscal approval for the non-automated tax flow, and an approved FX/fee process for COP. Until those exist, leave the provider absent from `BILLING_APPROVED_PROVIDERS` and keep billing enforcement disabled.

## Privacy boundary

Store opaque provider/order IDs, event hashes, financial state, deployment IDs, timestamps, and audit actors. Do not store card data, raw webhook payloads, chats, phone numbers, JIDs, QR data, session files, or dLocal payer name/email/document/phone in the control plane.
