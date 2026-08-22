from __future__ import annotations

import app as app_module


class FakeBillingGate:
    def decision(self):
        return {
            "status": "active",
            "observed_allowed": True,
            "service_allowed": True,
            "enforcement": True,
            "paid_through": "2026-09-20T00:00:00Z",
            "grace_until": None,
            "override_until": None,
        }

    def billing_status(self):
        return {
            "ok": True,
            "billing": {
                "status": "active",
                "service_allowed": True,
                "paid_through": "2026-09-20T00:00:00Z",
                "grace_until": None,
                "override_until": None,
                "provider_customer_configured": True,
            },
            "commercial": {
                "amount": 25000,
                "currency": "eur",
                "tax_automation": False,
                "billing_interval": "month",
            },
            "provider": {"name": "stripe", "compliance_approved": True},
            "payment_methods": [{
                "id": "stripe-card-sepa",
                "provider": "stripe",
                "label": "Tarjeta / SEPA",
                "recurring": True,
                "customer_currency": "EUR",
                "requires_payer": False,
                "requires_phone": False,
            }],
            "audit": [],
        }

    def create_checkout(self, *, method_id: str = "", payer: dict | None = None):
        assert method_id == "stripe-card-sepa"
        assert payer == {}
        return {"ok": True, "url": "https://checkout.stripe.invalid/session", "action": "redirect"}

    def create_portal(self):
        return {"ok": True, "url": "https://billing.stripe.invalid/session"}

    def create_override(self, *, reason: str, duration_seconds: int):
        return {"override": {"id": 1, "reason": reason, "duration": duration_seconds}}


def client(monkeypatch):
    monkeypatch.setattr(app_module, "billing_gate", FakeBillingGate())
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def test_billing_status_requires_authorized_panel(monkeypatch):
    test_client = client(monkeypatch)
    monkeypatch.setattr(app_module, "_can_manage_channels", lambda: False)
    assert test_client.get("/api/billing/status").status_code == 403


def test_billing_status_is_sanitized_and_exact_price(monkeypatch):
    test_client = client(monkeypatch)
    monkeypatch.setattr(app_module, "_can_manage_channels", lambda: True)
    response = test_client.get("/api/billing/status")
    assert response.status_code == 200
    assert response.json["commercial"]["amount"] == 25000
    assert response.json["commercial"]["currency"] == "eur"
    assert "token" not in str(response.json).lower()


def test_checkout_and_override_use_panel_mutation_guard(monkeypatch):
    test_client = client(monkeypatch)
    monkeypatch.setattr(app_module, "_channel_mutation_error", lambda: None)
    checkout = test_client.post(
        "/api/billing/checkout", json={"method_id": "stripe-card-sepa"}
    )
    override = test_client.post(
        "/api/billing/override",
        json={"reason": "Provider incident under review", "duration_seconds": 3600},
    )
    too_long = test_client.post(
        "/api/billing/override",
        json={"reason": "Provider incident under review", "duration_seconds": 86401},
    )
    assert checkout.status_code == 200
    assert checkout.json["url"].startswith("https://")
    assert override.status_code == 200
    assert too_long.status_code == 400


def test_admin_template_contains_billing_controls():
    assert 'id="billing-card"' in app_module.TEMPLATE
    assert "€250 al mes" in app_module.TEMPLATE
    assert "/api/billing/checkout" not in app_module.TEMPLATE
    assert "`/api/billing/${action}`" in app_module.TEMPLATE
    assert 'id="billing-method-select"' in app_module.TEMPLATE
    assert 'id="billing-payer-fields"' in app_module.TEMPLATE
