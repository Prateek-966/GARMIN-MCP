"""
Run this ONCE, on your own machine. Never on the server.

It logs into Garmin Connect (handling MFA if your account has it), then prints a
base64 token blob. Paste that into the GARMIN_TOKENS environment variable on
Render. Your password is used here and then discarded — it is never stored,
transmitted to the server, or committed.

Usage:
    pip install garminconnect
    python auth_setup.py
"""

import base64
import getpass
import json
import sys

from garminconnect import Garmin


def main() -> int:
    print("Garmin Connect login (credentials stay on this machine)\n")
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")

    def prompt_mfa() -> str:
        return input("MFA code: ").strip()

    client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)

    try:
        client.login()
    except Exception as exc:  # noqa: BLE001
        print(f"\nLogin failed: {exc}", file=sys.stderr)
        print(
            "\nIf this is a 403 or a Cloudflare block, wait a few minutes and retry — "
            "Garmin rate-limits repeated login attempts.",
            file=sys.stderr,
        )
        return 1

    # garminconnect >= 0.3 serializes the session itself (di_token,
    # di_refresh_token, di_client_id) as a JSON string. Base64 it so the value
    # survives being pasted into an environment variable field as one line,
    # with no quoting or newline hazards.
    token_json = client.client.dumps()

    try:
        keys = set(json.loads(token_json))
    except json.JSONDecodeError:
        keys = set()
    if "di_refresh_token" not in keys:
        print(
            "\nLogin reported success but no refresh token was returned. "
            "Re-run and check for an MFA prompt you may have missed.",
            file=sys.stderr,
        )
        return 1

    blob = base64.b64encode(token_json.encode()).decode()

    print(f"\nLogged in as: {client.get_full_name()}")
    print("\n" + "=" * 70)
    print("GARMIN_TOKENS — copy everything between the markers, one single line:")
    print("=" * 70)
    print(blob)
    print("=" * 70)
    print(
        "\nSet this as the GARMIN_TOKENS env var on Render (Environment tab).\n"
        "The refresh token is good for roughly a year; when calls start failing "
        "with an auth error, re-run this script and replace the value.\n"
        "Do not paste this into a file you might commit — it is equivalent to "
        "your Garmin session."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
