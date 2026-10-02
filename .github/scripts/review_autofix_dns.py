"""Keep hosted-runner DNS independent of systemd-resolved during drop-sudo."""

import argparse
import ipaddress
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


HOSTS = ("github.com", "api.github.com", "results-receiver.actions.githubusercontent.com")


def upstream_config(text):
    servers = []
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        if not fields or fields[0] != "nameserver":
            continue
        if len(fields) != 2:
            raise ValueError("Malformed upstream DNS configuration")
        address = ipaddress.ip_address(fields[1])
        address = getattr(address, "ipv4_mapped", None) or address
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError("Upstream DNS must not depend on a local stub resolver")
        servers.append(fields[1])
    if not servers:
        raise ValueError("No existing upstream DNS servers; refusing to invent a fallback")
    return text.rstrip() + "\n"


def hosts_config(text):
    lines = text.splitlines(keepends=True)
    indexes = [i for i, line in enumerate(lines) if re.match(r"^\s*hosts\s*:", line)]
    if len(indexes) != 1:
        raise ValueError("Expected exactly one hosts entry in nsswitch.conf")
    # nss-resolve can bypass resolv.conf and contact the broken daemon directly.
    # Keep /etc/hosts, then query the runner's existing upstream DNS servers.
    lines[indexes[0]] = "hosts: files dns\n"
    return "".join(lines)


def replace_file(path, text):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temporary:
        candidate = Path(temporary.name)
        try:
            temporary.write(text)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o644)
            os.replace(candidate, path)
        finally:
            candidate.unlink(missing_ok=True)


def configure(upstream, resolver, nsswitch):
    # Validate both inputs before replacing either file. Copy rather than retain
    # a symlink to systemd-resolved's files, which disappear when it stops.
    dns = upstream_config(upstream.read_text())
    hosts = hosts_config(nsswitch.read_text())
    replace_file(resolver, dns)
    replace_file(nsswitch, hosts)
    print("DNS now uses this hosted runner's existing upstream servers directly.", flush=True)


def check():
    for host in HOSTS:
        subprocess.run([sys.executable, "-c",
                        "import socket,sys; socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)",
                        host], check=True, timeout=10)
        print(f"DNS lookup succeeded: {host}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["configure", "check"])
    args = parser.parse_args()
    if args.operation == "configure":
        if (sys.platform != "linux" or os.geteuid() != 0 or
                os.environ.get("GITHUB_ACTIONS") != "true" or
                os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"):
            raise RuntimeError("DNS configuration is restricted to root on disposable GitHub-hosted Linux runners")
        configure(Path("/run/systemd/resolve/resolv.conf"), Path("/etc/resolv.conf"),
                  Path("/etc/nsswitch.conf"))
    else:
        check()


if __name__ == "__main__":
    main()
