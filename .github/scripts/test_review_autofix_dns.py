import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import review_autofix_dns as dns


class DnsTests(unittest.TestCase):
    def test_uses_existing_servers_and_search_domains_without_public_fallback(self):
        text = "# runner DHCP configuration\nnameserver 168.63.129.16\nsearch internal.example\noptions edns0\n"
        self.assertEqual(dns.upstream_config(text), text)

    def test_missing_invalid_and_stub_servers_fail_closed(self):
        for text in ["# empty", "nameserver invalid", "nameserver 127.0.0.53", "nameserver ::1",
                     "nameserver ::ffff:127.0.0.53", "nameserver 0.0.0.0", "nameserver 224.0.0.1",
                     "nameserver 168.63.129.16\nnameserver 127.0.0.53"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                dns.upstream_config(text)

    def test_native_lookups_bypass_nss_resolve_and_preserve_other_databases(self):
        self.assertEqual(dns.hosts_config("passwd: files\nhosts: files resolve [!UNAVAIL=return] dns\ngroup: files\n"),
                         "passwd: files\nhosts: files dns\ngroup: files\n")
        for text in ["passwd: files\n", "hosts: files\nhosts: dns\n"]:
            with self.assertRaises(ValueError):
                dns.hosts_config(text)

    def test_configuration_survives_removal_of_resolved_runtime_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream, stub, resolver, nsswitch = [root / name for name in ["upstream", "stub", "resolv.conf", "nsswitch.conf"]]
            upstream.write_text("nameserver 168.63.129.16\n")
            stub.write_text("nameserver 127.0.0.53\n")
            resolver.symlink_to(stub)
            nsswitch.write_text("hosts: resolve [!UNAVAIL=return] files dns\n")
            dns.configure(upstream, resolver, nsswitch)
            upstream.unlink(); stub.unlink()
            self.assertFalse(resolver.is_symlink())
            self.assertEqual(resolver.read_text(), "nameserver 168.63.129.16\n")
            self.assertEqual(nsswitch.read_text(), "hosts: files dns\n")
            self.assertEqual(resolver.stat().st_mode & 0o777, 0o644)

    def test_invalid_nss_configuration_does_not_change_resolver(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream, resolver, nsswitch = [root / name for name in ["upstream", "resolv.conf", "nsswitch.conf"]]
            upstream.write_text("nameserver 168.63.129.16\n")
            resolver.write_text("nameserver 127.0.0.53\n")
            nsswitch.write_text("unexpected")
            with self.assertRaises(ValueError):
                dns.configure(upstream, resolver, nsswitch)
            self.assertEqual(resolver.read_text(), "nameserver 127.0.0.53\n")

    def test_cli_refuses_to_reconfigure_other_machines(self):
        with patch.dict(os.environ, {"RUNNER_ENVIRONMENT": "self-hosted"}), \
                patch("sys.argv", ["dns", "configure"]), patch.object(dns, "configure") as configure:
            with self.assertRaisesRegex(RuntimeError, "disposable"):
                dns.main()
            configure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
