#!/usr/bin/env python3
"""Copy selected environment secrets into a GitHub Actions repository."""

from __future__ import annotations

import argparse
import base64
import os

import requests
from nacl.public import PublicKey, SealedBox


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repository", help="owner/repository")
    args = parser.parse_args()
    token = os.environ.get("PAT_TOKEN", "").strip()
    if not token:
        raise SystemExit("PAT_TOKEN is not set")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base = f"https://api.github.com/repos/{args.repository}/actions/secrets"
    response = requests.get(f"{base}/public-key", headers=headers, timeout=30)
    response.raise_for_status()
    key_info = response.json()
    public_key = PublicKey(base64.b64decode(key_info["key"]))
    box = SealedBox(public_key)
    values = {
        "GMAIL_USER": os.environ.get("GMAIL_USER", "").strip(),
        "GMAIL_APP_PASSWORD": os.environ.get("GMAIL_APP_PASSWORD", "").strip(),
        "ALERT_TO": os.environ.get("ALERT_TO", "").strip() or os.environ.get("GMAIL_USER", "").strip(),
        "CL_TOKEN": os.environ.get("CL_TOKEN", "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    for name, value in values.items():
        encrypted = base64.b64encode(box.encrypt(value.encode())).decode()
        result = requests.put(
            f"{base}/{name}", headers=headers,
            json={"encrypted_value": encrypted, "key_id": key_info["key_id"]}, timeout=30,
        )
        result.raise_for_status()
        print(f"Configured {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

