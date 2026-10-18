#!/usr/bin/env python3
"""Store, check or remove the Quip token in the macOS Keychain.

The token is never printed, logged or written to disk by this tool.

    python secrets_setup.py set      # how to get a token, then a hidden prompt
    python secrets_setup.py status   # shows present/absent only
    python secrets_setup.py clear    # removes it
"""
import argparse
import getpass
import sys

import keyring
import keyring.errors

SERVICE = "quip-migration"
QUIP_TOKEN = "quip-token"

INSTRUCTIONS = """\
Get a Quip personal access token:
  1. Sign in to Quip in your browser.
  2. Open https://quip.com/dev/token (company site: https://<yourcompany>.quip.com/dev/token)
  3. Generate a token and copy it. No button or an error? Ask your Quip admin to enable API access.
"""


def require_token():
    """Return the stored token. Callers must never print it."""
    token = keyring.get_password(SERVICE, QUIP_TOKEN)
    if not token:
        sys.exit("No Quip token in Keychain. Run: python secrets_setup.py set")
    return token


def cmd_set():
    print(INSTRUCTIONS)
    token = getpass.getpass("Paste the token (input hidden): ").strip()
    if not token:
        sys.exit("No token entered; nothing stored.")
    keyring.set_password(SERVICE, QUIP_TOKEN, token)
    print("Stored in Keychain. Clear your clipboard:  pbcopy < /dev/null")


def cmd_status():
    print("present" if keyring.get_password(SERVICE, QUIP_TOKEN) else "absent")


def cmd_clear():
    try:
        keyring.delete_password(SERVICE, QUIP_TOKEN)
        print("Removed.")
    except keyring.errors.PasswordDeleteError:
        print("Nothing stored.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["set", "status", "clear"])
    {"set": cmd_set, "status": cmd_status, "clear": cmd_clear}[parser.parse_args().command]()


if __name__ == "__main__":
    main()
