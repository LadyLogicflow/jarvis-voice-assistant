"""Unit tests for the Einkauf triage heuristic (Issue #230).

Tests cover _is_einkauf_mail() (pure function, no IO) and the route()
integration to verify the correct action dict is returned.
"""

from __future__ import annotations

import pytest

import mail_triage


# ---------------------------------------------------------------------------
# _is_einkauf_mail — pure unit tests
# ---------------------------------------------------------------------------


class TestIsEinkaufMail:
    """Tests for mail_triage._is_einkauf_mail()."""

    # PayPal: always einkauf regardless of subject
    def test_paypal_de_always_einkauf(self):
        assert mail_triage._is_einkauf_mail(
            "service@paypal.de", "Ihr Konto wurde aufgeladen"
        ) is True

    def test_paypal_com_always_einkauf(self):
        assert mail_triage._is_einkauf_mail(
            "service@paypal.com", "Payment received from seller"
        ) is True

    def test_paypal_no_subject_keyword_still_einkauf(self):
        """PayPal mails need no subject keyword."""
        assert mail_triage._is_einkauf_mail("noreply@paypal.de", "") is True

    # Amazon order confirmations
    def test_amazon_de_bestellbestaetigung(self):
        assert mail_triage._is_einkauf_mail(
            "auto-confirm@amazon.de", "Ihre Bestellbestätigung"
        ) is True

    def test_amazon_com_order_confirmation(self):
        assert mail_triage._is_einkauf_mail(
            "shipment-tracking@amazon.com", "Your order confirmation #123"
        ) is True

    def test_amazon_versandbestaetigung(self):
        assert mail_triage._is_einkauf_mail(
            "auto-confirm@amazon.de", "Versandbestätigung für Ihre Bestellung"
        ) is True

    def test_amazon_wurde_versandt(self):
        assert mail_triage._is_einkauf_mail(
            "auto-confirm@amazon.de", "Ihre Bestellung wurde versandt"
        ) is True

    # Amazon marketing: must NOT be matched
    def test_amazon_marketing_not_einkauf(self):
        assert mail_triage._is_einkauf_mail(
            "store-news@amazon.de", "Angebote des Tages — 50% Rabatt!"
        ) is False

    def test_amazon_deal_not_einkauf(self):
        assert mail_triage._is_einkauf_mail(
            "noreply@amazon.de", "Blitzangebot: Heute nur!"
        ) is False

    # Zalando
    def test_zalando_bestellbestaetigung(self):
        assert mail_triage._is_einkauf_mail(
            "no-reply@zalando.de", "Deine Bestellbestätigung"
        ) is True

    def test_zalando_deine_bestellung(self):
        assert mail_triage._is_einkauf_mail(
            "order@zalando.de", "Deine Bestellung ist unterwegs"
        ) is True

    # Otto
    def test_otto_bestellbestaetigung(self):
        assert mail_triage._is_einkauf_mail(
            "noreply@otto.de", "Ihre Bestellbestätigung"
        ) is True

    # eBay
    def test_ebay_payment_confirmed(self):
        assert mail_triage._is_einkauf_mail(
            "auto@ebay.de", "Payment confirmed for your purchase"
        ) is True

    # DPD
    def test_dpd_versandbestaetigung(self):
        assert mail_triage._is_einkauf_mail(
            "noreply@dpd.de", "Versandbestätigung Ihre Sendung"
        ) is True

    # Unknown domain: must NOT be matched
    def test_unknown_domain_not_einkauf(self):
        assert mail_triage._is_einkauf_mail(
            "info@meinshop.de", "Ihre Bestellung wurde versandt"
        ) is False

    # Case-insensitivity
    def test_subject_keyword_case_insensitive(self):
        assert mail_triage._is_einkauf_mail(
            "confirm@amazon.de", "BESTELLBESTÄTIGUNG Nr. 1234"
        ) is True

    def test_sender_case_insensitive(self):
        assert mail_triage._is_einkauf_mail(
            "Service@PayPal.DE", "Zahlungsbestätigung erhalten"
        ) is True

    # English keywords
    def test_has_been_shipped(self):
        assert mail_triage._is_einkauf_mail(
            "tracking@ebay.com", "Your item has been shipped"
        ) is True

    def test_your_order(self):
        assert mail_triage._is_einkauf_mail(
            "noreply@otto.de", "Your order #ABC has been received"
        ) is True

    # Zahlungsbestätigung
    def test_zahlungsbestaetigung(self):
        assert mail_triage._is_einkauf_mail(
            "noreply@paypal.de", "Zahlungsbestätigung"
        ) is True

    def test_zahlung_erhalten(self):
        assert mail_triage._is_einkauf_mail(
            "noreply@paypal.de", "Zahlung erhalten"
        ) is True


# ---------------------------------------------------------------------------
# route() integration — einkauf action returned
# ---------------------------------------------------------------------------


class TestRouteEinkauf:
    """Integration tests verifying that route() returns the einkauf action."""

    def test_route_paypal_returns_einkauf_action(self):
        result = mail_triage.route(
            sender="PayPal <service@paypal.de>",
            subject="Zahlung erhalten",
            category="info",
        )
        assert result["action"] == "einkauf"
        assert "folder" in result

    def test_route_amazon_order_returns_einkauf_action(self):
        result = mail_triage.route(
            sender="Amazon.de <auto-confirm@amazon.de>",
            subject="Ihre Bestellbestätigung",
            category="info",
        )
        assert result["action"] == "einkauf"

    def test_route_amazon_marketing_does_not_return_einkauf(self):
        """Amazon marketing mails must fall through to normal werbung path."""
        result = mail_triage.route(
            sender="Amazon.de <noreply@amazon.de>",
            subject="Angebote des Tages — bis zu 60% Rabatt",
            category="werbung",
        )
        assert result["action"] != "einkauf"

    def test_route_unknown_sender_order_subject_not_einkauf(self):
        """Order-like subject from an unknown domain must NOT match."""
        result = mail_triage.route(
            sender="Shop <info@unknownshop.de>",
            subject="Ihre Bestellbestätigung",
            category="info",
        )
        assert result["action"] != "einkauf"

    def test_route_einkauf_uses_default_folder(self):
        """When no einkauf_folder is in the rules file, INBOX.Einkauf is used."""
        result = mail_triage.route(
            sender="PayPal <service@paypal.de>",
            subject="Zahlung erhalten",
            category="info",
        )
        assert result["action"] == "einkauf"
        # Default folder from _load_rules() fallback
        assert result.get("folder", "INBOX.Einkauf") == "INBOX.Einkauf"
