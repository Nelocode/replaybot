# Entitlement state machine

| Input | Result | Access |
|---|---|---|
| New eligible account | `trialing` until day 7 | Yes |
| Valid EUR 250 recurring provider payment | `active` through paid period | Yes |
| Correlated PSE/APM/crypto order paid in final state | `active` for one calendar month | Yes |
| Payment failed or paid period expired | `grace` for 72 hours | Yes |
| Grace expired | `suspended` | No |
| Refund or chargeback | `suspended` immediately | No |
| Cancellation | `canceled` | Only until paid-through |
| Audited override | underlying state unchanged | Yes, at most 24 hours |

Never extend `paid_through` from a subscription-update, pending crypto confirmation, redirect return, or browser claim. Only a verified recurring payment or a final correlated payment order can extend it. Stale failures, updates, and cancellations are ignored; stale refunds and chargebacks are not.
