"""Cross-cutting safety guards.

* `no_live_sends()` — context in which nothing may be sent or submitted. The
  pipeline, the daemon and the UI's "Run Pipeline" wrap their whole run in it,
  so LIVE configuration alone can never make them send; only an explicit
  submit action (`python main.py submit --app-id N`, the UI submit button)
  runs outside it.
* `check_public_url()` — an application URL must be plain http(s) on a public
  host: no localhost, private/reserved IPs, credentials or internal names.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from contextlib import contextmanager
from typing import Tuple
from urllib.parse import urlparse

_state = threading.local()


@contextmanager
def no_live_sends():
    """Everything inside runs with LIVE submission/sending disabled (re-entrant)."""
    depth = getattr(_state, "depth", 0)
    _state.depth = depth + 1
    try:
        yield
    finally:
        _state.depth = depth


def live_sends_forbidden() -> bool:
    return getattr(_state, "depth", 0) > 0


_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet", ".test",
                      ".invalid", ".example", ".onion")


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_global and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                                 or ip.is_reserved or ip.is_unspecified)


def check_public_url(url: str) -> Tuple[bool, str]:
    """(True, "") for a plain public http(s) URL, else (False, why). No network is used."""
    url = (url or "").strip()
    try:
        parts = urlparse(url)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return False, "malformed URL"
    if parts.scheme not in ("http", "https"):
        return False, f"scheme '{parts.scheme or 'none'}' is not http(s)"
    if not host:
        return False, "no host"
    if parts.username or parts.password or "@" in parts.netloc:
        return False, "URL carries credentials"
    if port not in (None, 80, 443):
        return False, f"non-standard port {port}"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return False, ("private/loopback/reserved IP address" if not _ip_is_public(ip)
                       else "bare IP address instead of a named employer/ATS host")
    if host == "localhost" or "." not in host or host.endswith(_INTERNAL_SUFFIXES):
        return False, "local/internal host name"
    if host.replace(".", "").isdigit() or host.startswith("0x"):
        return False, "numeric host"
    return True, ""


def is_safe_public_url(url: str) -> bool:
    return check_public_url(url)[0]


def resolves_to_public(url: str) -> Tuple[bool, str]:
    """DNS check used right before a real fetch/browser visit: every address the
    host resolves to must be public (defeats names pointing at 127.0.0.1/10.x)."""
    ok, why = check_public_url(url)
    if not ok:
        return False, why
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        return False, f"host does not resolve ({e.__class__.__name__})"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False, "unparseable address"
        if not _ip_is_public(ip):
            return False, "host resolves to a private/loopback address"
    return True, ""
