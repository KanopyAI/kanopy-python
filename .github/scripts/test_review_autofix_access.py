from email.message import Message
from io import BytesIO
import unittest
from unittest.mock import patch
import urllib.error

import review_autofix_access as access


def response(service):
    body = BytesIO(f"001f# service={service}\n0000".encode())
    body.headers = Message()
    body.headers["Content-Type"] = f"application/x-{service}-advertisement"
    return body


class AccessTests(unittest.TestCase):
    def test_write_permission_is_checked_even_when_reads_succeed(self):
        denied = urllib.error.HTTPError("https://github.com/o/r.git/info/refs", 403, "Forbidden", {}, None)
        with patch.object(access.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = [response("git-upload-pack"), denied]
            with self.assertRaisesRegex(RuntimeError, "git-receive-pack.*HTTP 403") as error:
                access.verify("o/r", "private-token-value")
            self.assertNotIn("private-token-value", str(error.exception))

    def test_preflight_only_reads_git_advertisements(self):
        with patch.object(access.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = [response("git-upload-pack"), response("git-receive-pack")]
            access.verify("o/r", "private-token-value")
            calls = build.return_value.open.call_args_list
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(c.args[0].get_method() == "GET" for c in calls))

    def test_login_pages_and_redirects_are_not_accepted(self):
        page = response("git-upload-pack")
        page.headers.replace_header("Content-Type", "text/html")
        with patch.object(access.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = page
            with self.assertRaisesRegex(RuntimeError, "authenticated Git advertisement"):
                access.verify("o/r", "private-token-value")
        with self.assertRaisesRegex(RuntimeError, "redirect"):
            access.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example/")


if __name__ == "__main__":
    unittest.main()
