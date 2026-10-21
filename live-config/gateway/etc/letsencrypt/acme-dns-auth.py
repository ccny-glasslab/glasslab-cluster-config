#!/usr/bin/env python3
"""certbot --manual-auth-hook for DNS-01 through acme-dns.

glasslab.org DNS is hosted at Squarespace, which has no API, and the CCNY
border resets Let's Encrypt's HTTP-01 requests before they reach the gateway.
So _acme-challenge.glasslab.org and _acme-challenge.www.glasslab.org are
CNAMEs (set once in Squarespace) to an acme-dns subdomain, and this hook
publishes each challenge value there. acme-dns keeps the two most recent TXT
values per subdomain, which covers glasslab.org + www.glasslab.org in one
certificate.

Credentials (from POST /register) live in /etc/letsencrypt/acmedns.json,
root-only. They are never tracked in the repo.
"""
import json
import os
import sys
import time
import urllib.request

CREDS = "/etc/letsencrypt/acmedns.json"
API = "https://auth.acme-dns.io/update"


def main() -> int:
    validation = os.environ["CERTBOT_VALIDATION"]
    domain = os.environ.get("CERTBOT_DOMAIN", "?")
    with open(CREDS) as fh:
        creds = json.load(fh)
    body = json.dumps({"subdomain": creds["subdomain"], "txt": validation}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Api-User": creds["username"],
            "X-Api-Key": creds["password"],
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            print(f"acme-dns update for {domain} failed: HTTP {resp.status}", file=sys.stderr)
            return 1
    # acme-dns answers authoritatively at once; a short pause covers its replicas.
    time.sleep(10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
