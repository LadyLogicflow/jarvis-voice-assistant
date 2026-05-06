"""Unit-Tests fuer extract_calendar_invite und format_calendar_when.

Testet die Kalender-Einladungserkennung (Issue #66) ohne Netzwerk-,
IMAP- oder API-Verbindungen. Alle Tests sind deterministisch.

Da mail_actions -> settings -> anthropic importiert, stubben wir settings
vor dem Import mit einem MagicMock (analog zu conftest._stub_env_and_config).
"""

from __future__ import annotations

import email
import email.mime.multipart
import email.mime.text
import email.mime.base
import sys
import textwrap
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# settings-Stub einschleusen BEVOR mail_actions importiert wird.
# Notwendig weil settings.py auf Modulebene `import anthropic` ausfuehrt.
# ---------------------------------------------------------------------------
if "settings" not in sys.modules:
    stub = ModuleType("settings")
    stub.log = MagicMock()
    stub.MAIL_MONITOR_ACCOUNTS = []
    sys.modules["settings"] = stub

import mail_actions  # noqa: E402 (nach stub-Setup)


# ---------------------------------------------------------------------------
# Hilfsfunktion: synthetische Mail mit ICS-Inhalt bauen
# ---------------------------------------------------------------------------

def _make_ics_mail(ics_content: str, as_attachment: bool = False) -> email.message.Message:
    """Baut eine multipart-Mail mit ICS-Inhalt.

    as_attachment=False -> text/calendar als Inline-Part (Outlook-Style)
    as_attachment=True  -> .ics-Anhang (Apple Mail Style)
    """
    msg = email.mime.multipart.MIMEMultipart()
    msg["From"] = "test@example.com"
    msg["To"] = "catrin@example.com"
    msg["Subject"] = "Testtermin"

    body = email.mime.text.MIMEText("Bitte beachten Sie die Einladung.", "plain", "utf-8")
    msg.attach(body)

    ics_bytes = ics_content.encode("utf-8")
    if as_attachment:
        att = email.mime.base.MIMEBase("application", "ics")
        att.set_payload(ics_bytes)
        att["Content-Disposition"] = 'attachment; filename="invite.ics"'
        msg.attach(att)
    else:
        cal_part = email.mime.base.MIMEBase("text", "calendar")
        cal_part.set_payload(ics_bytes)
        msg.attach(cal_part)

    return msg


ICS_SIMPLE = textwrap.dedent("""\
    BEGIN:VCALENDAR
    VERSION:2.0
    BEGIN:VEVENT
    SUMMARY:Jahresabschluss-Meeting
    DTSTART:20260507T140000
    DTEND:20260507T150000
    LOCATION:Konferenzraum A
    ORGANIZER:mailto:chef@example.com
    END:VEVENT
    END:VCALENDAR
""")

ICS_ALLDAY = textwrap.dedent("""\
    BEGIN:VCALENDAR
    VERSION:2.0
    BEGIN:VEVENT
    SUMMARY:Jahrestag
    DTSTART:20260601
    DTEND:20260602
    END:VEVENT
    END:VCALENDAR
""")

ICS_FOLDED = textwrap.dedent("""\
    BEGIN:VCALENDAR
    VERSION:2.0
    BEGIN:VEVENT
    SUMMARY:Langer Titel der umgebrochen
     wird in der ICS-Datei
    DTSTART:20260510T090000
    DTEND:20260510T100000
    END:VEVENT
    END:VCALENDAR
""")


# ---------------------------------------------------------------------------
# extract_calendar_invite Tests
# ---------------------------------------------------------------------------

class TestExtractCalendarInvite:
    def test_inline_calendar_part(self):
        msg = _make_ics_mail(ICS_SIMPLE, as_attachment=False)
        result = mail_actions.extract_calendar_invite(msg)
        assert result is not None
        assert result["summary"] == "Jahresabschluss-Meeting"
        assert result["dtstart"] == "20260507T140000"
        assert result["dtend"] == "20260507T150000"
        assert result["location"] == "Konferenzraum A"

    def test_ics_attachment(self):
        msg = _make_ics_mail(ICS_SIMPLE, as_attachment=True)
        result = mail_actions.extract_calendar_invite(msg)
        assert result is not None
        assert result["summary"] == "Jahresabschluss-Meeting"

    def test_allday_event(self):
        msg = _make_ics_mail(ICS_ALLDAY, as_attachment=False)
        result = mail_actions.extract_calendar_invite(msg)
        assert result is not None
        assert result["dtstart"] == "20260601"

    def test_line_folding(self):
        """RFC 5545 Zeilenumbrueche (Folding) muessen korrekt zusammengefuehrt werden."""
        msg = _make_ics_mail(ICS_FOLDED, as_attachment=False)
        result = mail_actions.extract_calendar_invite(msg)
        assert result is not None
        # Beide Teile des gefalteten Titels muessen enthalten sein
        assert "Langer Titel" in result["summary"]
        assert "umgebrochen" in result["summary"]

    def test_no_ics_returns_none(self):
        msg = email.mime.text.MIMEText("Normale Mail ohne Kalender.", "plain", "utf-8")
        result = mail_actions.extract_calendar_invite(msg)
        assert result is None

    def test_none_input(self):
        result = mail_actions.extract_calendar_invite(None)
        assert result is None

    def test_empty_ics_returns_none(self):
        """Ein ICS ohne SUMMARY und DTSTART soll None ergeben."""
        ics = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nEND:VEVENT\nEND:VCALENDAR\n"
        msg = _make_ics_mail(ics, as_attachment=False)
        result = mail_actions.extract_calendar_invite(msg)
        assert result is None

    def test_organizer_extracted(self):
        msg = _make_ics_mail(ICS_SIMPLE, as_attachment=False)
        result = mail_actions.extract_calendar_invite(msg)
        assert result is not None
        assert "chef@example.com" in result.get("organizer", "")


# ---------------------------------------------------------------------------
# format_calendar_when Tests
# ---------------------------------------------------------------------------

class TestFormatCalendarWhen:
    def test_datetime_format(self):
        human = mail_actions.format_calendar_when("20260507T140000")
        assert "7." in human
        assert "Mai" in human
        assert "14:00" in human

    def test_datetime_utc_z(self):
        human = mail_actions.format_calendar_when("20260507T120000Z")
        assert "7." in human
        assert "Mai" in human

    def test_date_only(self):
        human = mail_actions.format_calendar_when("20260601")
        assert "1." in human
        assert "Juni" in human
        # Keine Uhrzeit bei reinem Datum
        assert "um" not in human

    def test_empty_string(self):
        assert mail_actions.format_calendar_when("") == ""

    def test_unknown_format_returns_input(self):
        raw = "TOTALLY-INVALID"
        assert mail_actions.format_calendar_when(raw) == raw

    @pytest.mark.parametrize("month_num,month_name", [
        ("01", "Januar"), ("02", "Februar"), ("03", "Maerz"),
        ("04", "April"),  ("05", "Mai"),     ("06", "Juni"),
        ("07", "Juli"),   ("08", "August"),  ("09", "September"),
        ("10", "Oktober"), ("11", "November"), ("12", "Dezember"),
    ])
    def test_all_months(self, month_num, month_name):
        human = mail_actions.format_calendar_when(f"2026{month_num}15T100000")
        assert month_name in human
