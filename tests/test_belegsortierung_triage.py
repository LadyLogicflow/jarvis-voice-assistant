"""Unit tests fuer BelegSortierung-Triage (Issue #234).

Testet:
- _is_steuerbeleg_mail() — pure Hilfsfunktion in mail_triage.py
- route() Integration — liefert {"action": "steuerbeleg"} wenn angemessen
- Keine Fehlklassifikation normaler Mails als steuerbeleg
"""

from __future__ import annotations

import email
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

import pytest

import mail_triage


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _make_msg_with_attachment(ext: str = ".pdf") -> email.message.Message:
    """Erstellt eine minimale Multipart-Mail mit einem Anhang."""
    msg = MIMEMultipart()
    msg.attach(MIMEText("Hier sind meine Unterlagen.", "plain"))
    part = MIMEBase("application", "pdf")
    part.set_payload(b"%PDF-1.4 test")
    part.add_header("Content-Disposition", "attachment",
                    filename=f"test{ext}")
    msg.attach(part)
    return msg


def _make_msg_without_attachment() -> email.message.Message:
    """Erstellt eine Mail ohne Anhang."""
    return MIMEText("Hallo, wie geht es Ihnen?", "plain")


# ---------------------------------------------------------------------------
# _is_steuerbeleg_mail — reine Unit-Tests
# ---------------------------------------------------------------------------

class TestIsSteuerbeleg:
    """Tests fuer mail_triage._is_steuerbeleg_mail()."""

    # Kein Anhang → niemals steuerbeleg, unabhaengig vom Absender/Betreff
    def test_no_attachment_never_steuerbeleg(self):
        with patch("mandanten.find_by_name", return_value=[{"name": "Max Mustermann"}]):
            assert mail_triage._is_steuerbeleg_mail(
                "Max Mustermann", "Steuererklärung", False
            ) is False

    def test_keyword_no_attachment_not_steuerbeleg(self):
        assert mail_triage._is_steuerbeleg_mail(
            "unknown@example.de", "Unterlagen für Steuererklärung", False
        ) is False

    # Bekannter Absender (Mandant) mit Anhang → steuerbeleg
    def test_known_member_with_attachment_is_steuerbeleg(self):
        mock_mandant = [{"name": "Klaus Fischer", "mitgliedsnr": "12345"}]
        with patch("mandanten.find_by_name", return_value=mock_mandant):
            assert mail_triage._is_steuerbeleg_mail(
                "Klaus Fischer", "Hier meine Unterlagen", True
            ) is True

    def test_known_member_display_name_extracted(self):
        """Anzeigename wird vor < korrekt extrahiert."""
        mock_mandant = [{"name": "Maria Schneider"}]
        with patch("mandanten.find_by_name", return_value=mock_mandant) as mock_fn:
            result = mail_triage._is_steuerbeleg_mail(
                "Maria Schneider <m.schneider@example.de>", "Belege", True
            )
            # find_by_name sollte mit "Maria Schneider" aufgerufen worden sein
            mock_fn.assert_called_once_with("Maria Schneider")
            assert result is True

    # Unbekannter Absender + Schluesselwort im Betreff + Anhang → steuerbeleg
    def test_unknown_sender_keyword_attachment_is_steuerbeleg(self):
        with patch("mandanten.find_by_name", return_value=[]):
            assert mail_triage._is_steuerbeleg_mail(
                "Fremder Absender", "Steuerbeleg für 2024", True
            ) is True

    def test_unknown_sender_steuererklarung_keyword(self):
        with patch("mandanten.find_by_name", return_value=[]):
            assert mail_triage._is_steuerbeleg_mail(
                "someone@example.de", "Steuererklärung Unterlagen", True
            ) is True

    def test_unknown_sender_lohnsteuerbescheinigung(self):
        with patch("mandanten.find_by_name", return_value=[]):
            assert mail_triage._is_steuerbeleg_mail(
                "info@firma.de", "Lohnsteuerbescheinigung 2024", True
            ) is True

    def test_unknown_sender_jahresabschluss(self):
        with patch("mandanten.find_by_name", return_value=[]):
            assert mail_triage._is_steuerbeleg_mail(
                "buchhaltung@firma.de", "Jahresabschluss Unterlagen", True
            ) is True

    def test_unknown_sender_steuerdokumente(self):
        with patch("mandanten.find_by_name", return_value=[]):
            assert mail_triage._is_steuerbeleg_mail(
                "user@example.de", "Steuerdokumente eingereicht", True
            ) is True

    def test_unknown_sender_belege(self):
        with patch("mandanten.find_by_name", return_value=[]):
            assert mail_triage._is_steuerbeleg_mail(
                "user@example.de", "Belege zum Jahresabschluss", True
            ) is True

    # Normale Mails ohne Schluesselwoerter und unbekannter Absender → kein steuerbeleg
    def test_regular_mail_not_steuerbeleg(self):
        with patch("mandanten.find_by_name", return_value=[]):
            assert mail_triage._is_steuerbeleg_mail(
                "newsletter@shop.de", "Unsere neusten Angebote!", True
            ) is False

    def test_regular_mail_no_keyword_no_member(self):
        with patch("mandanten.find_by_name", return_value=[]):
            assert mail_triage._is_steuerbeleg_mail(
                "info@beispiel.de", "Hallo, ich wollte mal kurz fragen", True
            ) is False

    # Schluesselwort-Matching case-insensitiv
    def test_keyword_case_insensitive(self):
        with patch("mandanten.find_by_name", return_value=[]):
            assert mail_triage._is_steuerbeleg_mail(
                "user@example.de", "STEUERERKLÄRUNG 2024", True
            ) is True

    def test_keyword_unterlagen_uppercase(self):
        with patch("mandanten.find_by_name", return_value=[]):
            assert mail_triage._is_steuerbeleg_mail(
                "user@example.de", "UNTERLAGEN FUER STEUERJAHR", True
            ) is True

    # Fehler in mandanten.find_by_name → kein Absturz, Keyword-Fallback
    def test_mandanten_exception_falls_back_to_keyword(self):
        with patch("mandanten.find_by_name", side_effect=Exception("DB-Fehler")):
            # Mit Schluesselwort im Betreff sollte es trotzdem True sein
            assert mail_triage._is_steuerbeleg_mail(
                "user@example.de", "Steuererklärung anbei", True
            ) is True

    def test_mandanten_exception_no_keyword_is_false(self):
        with patch("mandanten.find_by_name", side_effect=Exception("DB-Fehler")):
            assert mail_triage._is_steuerbeleg_mail(
                "user@example.de", "Guten Morgen", True
            ) is False


# ---------------------------------------------------------------------------
# route() Integration — steuerbeleg-Action wird korrekt zurueckgegeben
# ---------------------------------------------------------------------------

class TestRouteSteuerbeleg:
    """Integration-Tests fuer route() mit steuerbeleg-Erkennung."""

    @pytest.fixture(autouse=True)
    def _enable_belegsortierung(self, monkeypatch):
        """Stellt sicher dass BELEGSORTIERUNG_API_URL gesetzt ist."""
        import settings as S
        monkeypatch.setattr(S, "BELEGSORTIERUNG_API_URL",
                            "https://test.belegsortierung.example.com")

    def test_route_known_member_pdf_returns_steuerbeleg(self):
        """Bekanntes Mitglied + PDF-Anhang → steuerbeleg-Action."""
        mock_mandant = [{"name": "Peter Meier", "mitgliedsnr": "99001"}]
        msg = _make_msg_with_attachment(".pdf")
        with patch("mandanten.find_by_name", return_value=mock_mandant):
            result = mail_triage.route(
                sender="Peter Meier <p.meier@example.de>",
                subject="Hallo, hier meine Unterlagen",
                category="handlungsbedarf",
                msg=msg,
            )
        assert result["action"] == "steuerbeleg"

    def test_route_unknown_sender_keyword_pdf_returns_steuerbeleg(self):
        """Unbekannter Absender + Keyword + PDF → steuerbeleg."""
        msg = _make_msg_with_attachment(".pdf")
        with patch("mandanten.find_by_name", return_value=[]):
            result = mail_triage.route(
                sender="Unbekannt <fremder@example.de>",
                subject="Steuerbeleg für Veranlagungsjahr 2024",
                category="info",
                msg=msg,
            )
        assert result["action"] == "steuerbeleg"

    def test_route_known_member_image_returns_steuerbeleg(self):
        """Bekanntes Mitglied + Bild-Anhang → steuerbeleg."""
        mock_mandant = [{"name": "Anna Schmidt", "mitgliedsnr": "88002"}]
        msg = _make_msg_with_attachment(".jpg")
        with patch("mandanten.find_by_name", return_value=mock_mandant):
            result = mail_triage.route(
                sender="Anna Schmidt",
                subject="Unterlagen wie besprochen",
                category="handlungsbedarf",
                msg=msg,
            )
        assert result["action"] == "steuerbeleg"

    def test_route_no_attachment_no_steuerbeleg(self):
        """Ohne Anhang kein steuerbeleg, auch bei Keyword."""
        mock_mandant = [{"name": "Hans Berger", "mitgliedsnr": "77003"}]
        msg = _make_msg_without_attachment()
        with patch("mandanten.find_by_name", return_value=mock_mandant):
            result = mail_triage.route(
                sender="Hans Berger",
                subject="Steuererklärung — Frage dazu",
                category="handlungsbedarf",
                msg=msg,
            )
        assert result["action"] != "steuerbeleg"

    def test_route_regular_mail_not_steuerbeleg(self):
        """Normale Mail ohne Keyword und unbekannter Absender → kein steuerbeleg."""
        msg = _make_msg_with_attachment(".pdf")
        with patch("mandanten.find_by_name", return_value=[]):
            result = mail_triage.route(
                sender="Newsletter <news@shop.de>",
                subject="Unsere Angebote diese Woche",
                category="werbung",
                msg=msg,
            )
        assert result["action"] != "steuerbeleg"

    def test_route_einkauf_not_reclassified_as_steuerbeleg(self):
        """Einkauf-Mails werden nicht als steuerbeleg reklassifiziert."""
        msg = _make_msg_with_attachment(".pdf")
        # Auch wenn Absender in Mandanten ist, Einkauf gewinnt zuerst
        with patch("mandanten.find_by_name", return_value=[]):
            result = mail_triage.route(
                sender="PayPal <service@paypal.de>",
                subject="Zahlungsbestätigung — Belege",  # Keyword + Einkauf-Sender
                category="info",
                msg=msg,
            )
        # Einkauf-Pruefung laeuft vor steuerbeleg → action muss einkauf sein
        assert result["action"] == "einkauf"

    def test_route_no_belegsortierung_url_no_steuerbeleg(self, monkeypatch):
        """Ohne konfigurierte API-URL wird steuerbeleg nie ausgeloest."""
        import settings as S
        monkeypatch.setattr(S, "BELEGSORTIERUNG_API_URL", "")
        mock_mandant = [{"name": "Klaus Fischer"}]
        msg = _make_msg_with_attachment(".pdf")
        with patch("mandanten.find_by_name", return_value=mock_mandant):
            result = mail_triage.route(
                sender="Klaus Fischer",
                subject="Steuerbeleg 2024",
                category="handlungsbedarf",
                msg=msg,
            )
        assert result["action"] != "steuerbeleg"
