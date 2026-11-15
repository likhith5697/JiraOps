"""Obtain a delegated refresh token for the OpsBridge service account (device-code flow).

Microsoft Graph does not allow application permissions to post channel messages, so replies are
sent as a dedicated service-account user. Run this ONCE, sign in as that service account, and put
the printed values into .env:

    python scripts/get_delegated_token.py --tenant <TENANT_ID> --client-id <CLIENT_ID>

Prerequisites (see docs/microsoft-setup.md):
  * App registration > Authentication > "Allow public client flows" = Yes
  * Delegated permission ChannelMessage.Send (+ offline_access, User.Read) consented
The refresh token is a credential: store it like a password and never commit it.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time

import httpx

try:  # read MICROSOFT_TENANT_ID / MICROSOFT_CLIENT_ID from opsbridge/.env (real env vars still win)
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
except ImportError:  # python-dotenv ships with pydantic-settings; without it, pass --tenant/--client-id
    pass

SCOPE = "https://graph.microsoft.com/ChannelMessage.Send offline_access openid profile"


def _jwt_claim(token: str, claim: str) -> str | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get(claim)
    except (IndexError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", default=os.getenv("MICROSOFT_TENANT_ID") or None,
                        required=not os.getenv("MICROSOFT_TENANT_ID"), help="defaults to MICROSOFT_TENANT_ID in .env")
    parser.add_argument("--client-id", default=os.getenv("MICROSOFT_CLIENT_ID") or None,
                        required=not os.getenv("MICROSOFT_CLIENT_ID"), help="defaults to MICROSOFT_CLIENT_ID in .env")
    parser.add_argument("--login-url", default="https://login.microsoftonline.com")
    args = parser.parse_args()

    base = f"{args.login_url}/{args.tenant}/oauth2/v2.0"
    with httpx.Client(timeout=30) as http:
        device = http.post(f"{base}/devicecode", data={"client_id": args.client_id, "scope": SCOPE})
        if device.status_code != 200:
            print("Device code request failed:", device.json().get("error_description", device.text), file=sys.stderr)
            return 1
        flow = device.json()
        print(flow["message"])  # "To sign in, use a web browser to open ... and enter the code ..."

        interval = int(flow.get("interval", 5))
        deadline = time.time() + int(flow.get("expires_in", 900))
        while time.time() < deadline:
            time.sleep(interval)
            response = http.post(f"{base}/token", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": args.client_id,
                "device_code": flow["device_code"],
            })
            body = response.json()
            if response.status_code == 200:
                user_oid = _jwt_claim(body.get("id_token", ""), "oid") or _jwt_claim(body["access_token"], "oid")
                print("\nSigned in. Add these to your .env (keep them secret):\n")
                print(f"MICROSOFT_DELEGATED_REFRESH_TOKEN={body['refresh_token']}")
                if user_oid:
                    print(f"MICROSOFT_SERVICE_ACCOUNT_USER_ID={user_oid}")
                return 0
            if body.get("error") == "slow_down":
                interval += 5
            elif body.get("error") != "authorization_pending":
                print("Sign-in failed:", body.get("error_description", body.get("error")), file=sys.stderr)
                return 1
    print("Timed out waiting for sign-in.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
