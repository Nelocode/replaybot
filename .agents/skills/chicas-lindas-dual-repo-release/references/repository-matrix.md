# Repository matrix

| Repository | City or role | Base branch | Protected state |
|---|---|---|---|
| `barcebot` | Barcelona bots and panel | `main` | WhatsApp auth, Telegram sessions, QR, `/app/data` |
| `replaybot` | Madrid bots and panel | `master` | WhatsApp auth, Telegram sessions, QR, `/app/data` |
| `billing-control-plane` | Shared billing state and signing | `main` | SQLite, signing private key, Stripe secrets |

The billing client modules are intentionally identical between bot repositories. The applications, entrypoints, worker topology, and panel implementations retain repository-specific differences.

Before a release, compare branch names, billing client hashes, dependency declarations, secret stripping, handler gates, persistent mounts, and rollback variables.
