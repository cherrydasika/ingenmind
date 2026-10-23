"""Requests to admin-configured addresses (outbound.py, R63)."""

import socket
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import outbound


def resolves_to(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return lambda *a, **k: [(family, socket.SOCK_STREAM, 6, "", (address, 443))]


class Outbound(unittest.TestCase):
    def blocked(self, url, address, redirected=False):
        with patch("socket.getaddrinfo", resolves_to(address)):
            try:
                outbound.check(url, redirected)
            except outbound.Blocked:
                return True
        return False

    def test_metadata_and_other_special_addresses_are_never_sent_to(self):
        for address in ("169.254.169.254", "fe80::1", "0.0.0.0", "224.0.0.1", "240.0.0.1", "::ffff:169.254.169.254"):
            self.assertTrue(self.blocked("https://hooks.example.com/x", address), address)

    def test_private_networks_are_allowed_for_intranets(self):
        for address in ("10.0.0.5", "172.18.0.3", "192.168.1.20", "93.184.216.34"):
            self.assertFalse(self.blocked("https://hooks.example.com/x", address), address)

    def test_loopback_only_when_the_url_names_localhost_and_never_through_a_redirect(self):
        self.assertFalse(self.blocked("http://localhost:8091/mcp", "127.0.0.1"))
        self.assertTrue(self.blocked("https://sneaky.example.com/", "127.0.0.1"))        # a name pointing home
        self.assertTrue(self.blocked("http://localhost:8091/", "127.0.0.1", redirected=True))

    def test_not_http_is_refused(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x"):
            with self.assertRaises(outbound.Blocked):
                outbound.check(url)

    def test_blocked_is_a_url_error_and_redirects_are_checked(self):
        self.assertTrue(issubclass(outbound.Blocked, urllib.error.URLError))     # callers' handling applies
        handler = outbound._CheckedRedirects()
        request = urllib.request.Request("https://hooks.example.com/x")
        with patch("socket.getaddrinfo", resolves_to("169.254.169.254")), self.assertRaises(outbound.Blocked):
            handler.redirect_request(request, None, 302, "Found", {}, "http://metadata.example/latest")
        with patch("socket.getaddrinfo", resolves_to("169.254.169.254")), self.assertRaises(outbound.Blocked):
            outbound.urlopen(request, timeout=1)                                  # checked before connecting


if __name__ == "__main__":
    unittest.main()
