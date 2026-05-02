"""Unit tests for browser_tools._is_safe_url (M1.4 URL whitelist)."""

from __future__ import annotations

import pytest

import browser_tools as bt


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://localhost:8080/foo",
        "  https://example.com  ",
        "https://sub.domain.example.co.uk/path?q=1#frag",
    ],
)
def test_is_safe_url_accepts_http_and_https(url):
    assert bt._is_safe_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<h1>x</h1>",
        "ftp://example.com",
        "",
        "not a url",
        "https://",          # no host
        "http:///path",      # no host
    ],
)
def test_is_safe_url_rejects_other_schemes(url):
    assert bt._is_safe_url(url) is False
