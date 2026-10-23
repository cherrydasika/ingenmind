"""Requests to addresses an admin configured (R63 security review): webhooks,
alert channels, MCP servers, OpenAPI tools and their token URLs, A2A agents.

The scraper's guard (dags/common/scraping/scraper.py) only fetches public
addresses, because anyone's question can lead to a page. These destinations
are chosen by an admin, so private networks are allowed (an intranet MCP
server, a webhook receiver on the same network), but never:

    link-local      169.254.0.0/16 and fe80::/10, the cloud metadata service
    multicast, reserved, unspecified
    loopback        unless the configured URL itself names localhost
                    (a local MCP server or agent); never through a redirect

Every address the host resolves to is checked, and every redirect again.
Blocked is a URLError, so callers' existing error handling applies.
"""

import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}


class Blocked(urllib.error.URLError):
    """The URL resolves to an address IngenMind doesn't send requests to."""


def check(url: str, redirected: bool = False) -> None:
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Blocked(f"not an http(s) URL: {url}")
    named_local = parts.hostname.lower() in LOCAL_NAMES and not redirected
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise urllib.error.URLError(f"cannot resolve {parts.hostname}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if ip.is_loopback:   # first: ::1 also counts as reserved
            if named_local:
                continue
            raise Blocked(f"{parts.hostname} resolves to {ip}, which IngenMind doesn't send requests to")
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise Blocked(f"{parts.hostname} resolves to {ip}, which IngenMind doesn't send requests to")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check(newurl, redirected=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects)


def urlopen(request, timeout: float):
    """urllib.request.urlopen, after checking the destination (and each redirect)."""
    check(request.full_url if isinstance(request, urllib.request.Request) else request)
    return _opener.open(request, timeout=timeout)
