import os
import tempfile
import unittest
from types import SimpleNamespace


os.environ.setdefault("CLAUDE_WEB_DATA_DIR", tempfile.mkdtemp(prefix="claude-web-test-"))

from claude_web import server  # noqa: E402


class MobileAccessLocalNetworkTest(unittest.TestCase):
    def request(self, direct_host, path="/", headers=None):
        return SimpleNamespace(
            client=SimpleNamespace(host=direct_host),
            headers=headers or {},
            url=SimpleNamespace(path=path),
        )

    def test_rfc1918_lan_clients_do_not_need_access_code(self):
        self.assertFalse(server._mobile_access_auth_required(self.request("192.168.1.82")))
        self.assertFalse(server._mobile_access_auth_required(self.request("10.0.0.8")))
        self.assertFalse(server._mobile_access_auth_required(self.request("172.16.2.9")))

    def test_ipv4_mapped_ipv6_lan_client_does_not_need_access_code(self):
        self.assertFalse(server._mobile_access_auth_required(self.request("::ffff:192.168.1.82")))

    def test_forwarded_lan_client_does_not_need_access_code(self):
        req = self.request("127.0.0.1", headers={"x-forwarded-for": "192.168.1.82"})
        self.assertFalse(server._mobile_access_auth_required(req))

    def test_public_forwarded_client_still_needs_access_code(self):
        req = self.request("127.0.0.1", headers={"x-forwarded-for": "8.8.8.8"})
        self.assertTrue(server._mobile_access_auth_required(req))


if __name__ == "__main__":
    unittest.main()
