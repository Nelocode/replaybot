from __future__ import annotations

import argparse
from pathlib import Path


def require(path: Path, fragments: list[str], failures: list[str]) -> None:
    if not path.is_file():
        failures.append(f"missing file: {path}")
        return
    text = path.read_text(encoding="utf-8")
    for fragment in fragments:
        if fragment not in text:
            failures.append(f"{path}: missing {fragment!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.workspace_root.resolve()
    failures: list[str] = []

    require(root / "billing-control-plane" / "billing_service" / "config.py", ["25_000", "72 * 60 * 60", "24 * 60 * 60", "7 * 24 * 60 * 60"], failures)
    require(root / "billing-control-plane" / "billing_service" / "api.py", ["/webhooks/stripe", "/webhooks/paypal", "/webhooks/dlocal", "/webhooks/bitpay", "/v1/entitlement", "/v1/admin/overrides", "payment_methods"], failures)
    require(root / "billing-control-plane" / "billing_service" / "db.py", ["CREATE TABLE IF NOT EXISTS payment_orders", "provider_order_id", "payer_amount_minor"], failures)
    require(root / "billing-control-plane" / "billing_service" / "service.py", ["ORDER_KINDS", "payment_order_mismatch", "payment_contract_mismatch", "add_calendar_month"], failures)
    for provider_file in ("stripe_provider.py", "paypal_provider.py", "dlocal_provider.py", "bitpay_provider.py"):
        require(root / "billing-control-plane" / "billing_service" / "providers" / provider_file, ["payment_methods", "parse_webhook"], failures)
    for repo in ("barcebot", "replaybot"):
        require(root / repo / "billing_entitlement.py", ["Ed25519PublicKey", "BILLING_ENFORCEMENT", "claims[\"exp\"]"], failures)
        require(root / repo / "billing_entitlement.mjs", ["EdDSA", "BILLING_ENFORCEMENT", "claims.exp"], failures)
        require(root / repo / "entrypoint.sh", ["BILLING_CONTROL_PLANE_ADMIN_TOKEN"], failures)
        require(root / repo / "app.py", ["billing-method-select", "payment_methods", "invalid_payer"], failures)

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    print("Billing entitlement contract verified across all three repositories.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
