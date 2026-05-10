"""iCloud CardDAV contact sync.

Fetches all contacts from iCloud and stores them in the local persons_db.
Requires ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD in .env.

App-specific password: appleid.apple.com → Security → App-Specific Passwords.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional

import httpx

import settings as S
from contacts import Contact

log = S.log

_PRINCIPAL_URL = "https://contacts.icloud.com"
_WELL_KNOWN = "https://contacts.icloud.com/.well-known/carddav"

_NS = {
    "d": "DAV:",
    "card": "urn:ietf:params:xml:ns:carddav",
}

# vCard field patterns.
# iCloud prefixes labeled fields with "item1.", "item2.", etc.
# — so TEL lines look like "item1.TEL;type=CELL:+49..." instead of "TEL:...".
_FN = re.compile(r"^(?:item\d+\.)?FN[;:](.+)$", re.MULTILINE)
_EMAIL = re.compile(r"^(?:item\d+\.)?EMAIL[^:]*:(.+)$", re.MULTILINE)
_TEL = re.compile(r"^(?:item\d+\.)?TEL[^:]*:(.+)$", re.MULTILINE)
_ORG = re.compile(r"^(?:item\d+\.)?ORG[;:](.+)$", re.MULTILINE)
_UID = re.compile(r"^(?:item\d+\.)?UID[;:](.+)$", re.MULTILINE)


def _unfold(vcard: str) -> str:
    """Remove vCard line folding (CRLF/LF + space/tab → nothing)."""
    return re.sub(r"\r?\n[ \t]", "", vcard)


def _parse_vcard(vcard: str) -> Optional[Contact]:
    vcard = _unfold(vcard)
    fn = _FN.search(vcard)
    if not fn:
        return None
    name = fn.group(1).strip()
    uid_m = _UID.search(vcard)
    uid = uid_m.group(1).strip() if uid_m else name
    emails = [m.group(1).strip() for m in _EMAIL.finditer(vcard)]
    phones = [m.group(1).strip() for m in _TEL.finditer(vcard)]
    org_m = _ORG.search(vcard)
    org = org_m.group(1).replace(";", " ").strip() if org_m else ""
    return Contact(id=uid, name=name, emails=emails, phones=phones, organization=org)


async def _discover_addressbook_url(client: httpx.AsyncClient) -> Optional[str]:
    """Walk the CardDAV discovery chain to find the default address book URL."""
    # 1. Follow .well-known redirect
    resp = await client.get(_WELL_KNOWN, follow_redirects=True)
    base = str(resp.url).rstrip("/")

    # 2. PROPFIND on principal to get address-book-home-set
    prop_xml = (
        '<?xml version="1.0"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
        "<d:prop><card:addressbook-home-set/></d:prop>"
        "</d:propfind>"
    )
    resp = await client.request(
        "PROPFIND", base, content=prop_xml,
        headers={"Depth": "0", "Content-Type": "application/xml"},
    )
    root = ET.fromstring(resp.text)
    href = root.find(".//card:addressbook-home-set/d:href", _NS)
    if href is None or not href.text:
        return None
    home = href.text.rstrip("/")
    if home.startswith("/"):
        parsed = httpx.URL(base)
        home = f"{parsed.scheme}://{parsed.host}{home}"

    # 3. PROPFIND on home to list address books
    resp = await client.request(
        "PROPFIND", home + "/",
        content='<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>',
        headers={"Depth": "1", "Content-Type": "application/xml"},
    )
    root = ET.fromstring(resp.text)
    for response in root.findall("d:response", _NS):
        rt = response.find(".//card:addressbook", _NS)
        if rt is not None:
            h = response.find("d:href", _NS)
            if h is not None and h.text:
                ab = h.text.rstrip("/")
                if ab.startswith("/"):
                    parsed = httpx.URL(base)
                    ab = f"{parsed.scheme}://{parsed.host}{ab}"
                return ab
    return home + "/card"


async def sync_icloud_contacts() -> list[Contact]:
    """Fetch all contacts from iCloud CardDAV. Returns list of Contact objects."""
    apple_id = S.ICLOUD_APPLE_ID
    app_pw = S.ICLOUD_APP_PASSWORD
    if not apple_id or not app_pw:
        log.warning("contacts_carddav: ICLOUD_APPLE_ID / ICLOUD_APP_PASSWORD not set — skipping")
        return []

    auth = (apple_id, app_pw)
    async with httpx.AsyncClient(auth=auth, timeout=30) as client:
        ab_url = await _discover_addressbook_url(client)
        if not ab_url:
            log.error("contacts_carddav: could not discover address book URL")
            return []

        log.info(f"contacts_carddav: fetching from {ab_url}")

        # REPORT to get all vCards
        report_xml = (
            '<?xml version="1.0"?>'
            '<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
            "<d:prop><d:getetag/><card:address-data/></d:prop>"
            "</card:addressbook-query>"
        )
        resp = await client.request(
            "REPORT", ab_url + "/",
            content=report_xml,
            headers={"Depth": "1", "Content-Type": "application/xml"},
        )

        root = ET.fromstring(resp.text)
        contacts: list[Contact] = []
        for response in root.findall("d:response", _NS):
            ad = response.find(".//card:address-data", _NS)
            if ad is not None and ad.text:
                c = _parse_vcard(ad.text)
                if c:
                    contacts.append(c)

    log.info(f"contacts_carddav: loaded {len(contacts)} contacts")
    return contacts
