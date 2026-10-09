"""Verify the saved publisher token before reserving a paid model attempt."""

import base64
import os
import re
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected redirect while checking GitHub Git access")


def verify(repo, token):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid GitHub repository")
    if not token or token != token.strip():
        raise ValueError("Set REVIEW_FIXER_TOKEN to a GitHub token without surrounding whitespace")
    credential = base64.b64encode(("x-access-token:" + token).encode()).decode()
    opener = urllib.request.build_opener(NoRedirect())
    for service in ("git-upload-pack", "git-receive-pack"):
        request = urllib.request.Request(
            f"https://github.com/{repo}.git/info/refs?service={service}",
            headers={"Authorization": "Basic " + credential,
                     "User-Agent": "review-autofix-access-check"})
        try:
            # GET only: authenticate read/write Git endpoints without fetching
            # PR code, creating a commit, or updating any repository ref.
            with opener.open(request, timeout=30) as response:
                if response.headers.get_content_type() != f"application/x-{service}-advertisement":
                    raise RuntimeError("GitHub did not return an authenticated Git advertisement")
                if f"# service={service}".encode() not in response.read(4096):
                    raise RuntimeError("GitHub returned an invalid Git advertisement")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"REVIEW_FIXER_TOKEN cannot access {service} for {repo} (HTTP {exc.code}). "
                "Check repository selection, Contents read/write, token expiry, and organization approval/SSO. "
                "Pull requests read/write is also required for review requests. No model attempt was reserved."
            ) from None
    print(f"REVIEW_FIXER_TOKEN authenticated Git read and write endpoints for {repo}; no refs changed.")


if __name__ == "__main__":
    verify(os.environ["GITHUB_REPOSITORY"], os.environ.get("REVIEW_FIXER_TOKEN", ""))
