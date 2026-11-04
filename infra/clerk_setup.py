#!/usr/bin/env python3
"""Set up a Clerk instance for the desk: who may sign in, what the session token says, and who is a head of desk.

    CLERK_SECRET_KEY=sk_live_... python3 infra/clerk_setup.py --head you@example.com --trader colleague@example.com

Idempotent; run it again whenever the desk's people change. It:

1. turns on Clerk's allowlist, so only the listed emails can sign up or sign in (an open instance would let anyone
   in, and the desk would treat them as a trader). An instance whose sign-up mode is already *restricted* is
   invitation only, which is tighter: it stays that way;
2. adds each --head and --trader email to the allowlist (or, invitation only, invites them with their role);
3. creates (or updates) the ``loandesk`` JWT template the web app asks for (VITE_CLERK_JWT_TEMPLATE): it carries
   ``trader`` (the username), ``email`` and ``role`` (from the user's public metadata), which the API reads;
4. sets ``public_metadata.role`` to ``head`` or ``trader`` for each listed person who has signed up. Someone who
   hasn't yet gets their role the next time this runs; until then the allowlist still keeps everyone else out.

The API then needs ``LOANDESK_TRADER_ROLES=["trader","head"]`` (the demo's terraform sets it) so a token without
one of those roles is refused. ``--revoke email`` takes someone off the allowlist and clears their role.
Uses only the standard library and the Clerk Backend API; the key never leaves this process.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = "https://api.clerk.com/v1"
TEMPLATE = "loandesk"
CLAIMS = {"trader": "{{user.username}}", "email": "{{user.primary_email_address}}",
          "role": "{{user.public_metadata.role}}"}


class ClerkError(SystemExit):
    def __init__(self, msg: str, code: int, detail: str) -> None:
        super().__init__(msg)
        self.code, self.detail = code, detail


def call(key: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(f"{API}{path}", method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": "loandesk-clerk-setup/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise ClerkError(f"Clerk {method} {path}: HTTP {e.code}: {detail}", e.code, detail) from e


def user_by_email(key: str, email: str) -> dict[str, Any] | None:
    users = call(key, "GET", f"/users?email_address={urllib.parse.quote(email)}")
    return users[0] if users else None


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="clerk_setup", description=__doc__.split("\n\n")[0])
    ap.add_argument("--head", action="append", default=[], help="a head of desk's email (repeat)")
    ap.add_argument("--trader", action="append", default=[], help="a trader's email (repeat)")
    ap.add_argument("--revoke", action="append", default=[], help="take this email off the desk (repeat)")
    args = ap.parse_args(argv)
    key = os.environ.get("CLERK_SECRET_KEY", "")
    if not key.startswith("sk_"):
        sys.exit("set CLERK_SECRET_KEY to the instance's secret key (sk_test_... or sk_live_...)")
    inst = call(key, "GET", "/instance")
    print(f"instance {inst['id']} ({inst['environment_type']})")

    try:
        call(key, "PATCH", "/instance/restrictions", {"allowlist": True})
        invite_only = False
        print("allowlist: on (only listed emails can sign up or sign in)")
    except ClerkError as e:
        if "sign_up_mode_restricted" not in e.detail:
            raise
        invite_only = True  # tighter still: nobody signs up without an invitation
        print("sign-up mode: restricted (invitation only); people join by invitation, their role on it")

    templates = {t["name"]: t for t in call(key, "GET", "/jwt_templates")}
    body = {"name": TEMPLATE, "claims": CLAIMS, "lifetime": 60, "allowed_clock_skew": 5}
    if TEMPLATE in templates:
        call(key, "PATCH", f"/jwt_templates/{templates[TEMPLATE]['id']}", body)
        print(f"JWT template {TEMPLATE}: updated")
    else:
        call(key, "POST", "/jwt_templates", body)
        print(f"JWT template {TEMPLATE}: created")

    listed = {} if invite_only else {x["identifier"].lower(): x for x in call(key, "GET", "/allowlist_identifiers")}
    people = {e.strip().lower(): "head" for e in args.head} | {e.strip().lower(): "trader" for e in args.trader
                                                              if e.strip().lower() not in args.head}
    for email, role in people.items():
        u = user_by_email(key, email)
        if not invite_only and email not in listed:
            call(key, "POST", "/allowlist_identifiers", {"identifier": email, "notify": False})
        if u is None and invite_only:  # Clerk emails them a sign-up link; the role is theirs when they use it
            call(key, "POST", "/invitations", {"email_address": email, "public_metadata": {"role": role},
                                               "notify": True, "ignore_existing": True})
            print(f"{email}: invited as {role} (Clerk emails the sign-up link)")
            continue
        if u is None:
            print(f"{email}: allowed in as {role}; not signed up yet, run this again after they do to set the role")
            continue
        meta = {**(u.get("public_metadata") or {}), "role": role}
        call(key, "PATCH", f"/users/{u['id']}/metadata", {"public_metadata": meta})
        print(f"{email}: allowed in, role {role}")
    for email in (e.strip().lower() for e in args.revoke):
        if email in listed:
            call(key, "DELETE", f"/allowlist_identifiers/{listed[email]['id']}")
        if invite_only:
            for inv in call(key, "GET", f"/invitations?status=pending&query={urllib.parse.quote(email)}") or []:
                call(key, "POST", f"/invitations/{inv['id']}/revoke")
        if (u := user_by_email(key, email)) is not None:
            call(key, "PATCH", f"/users/{u['id']}/metadata", {"public_metadata": {"role": None}})
        print(f"{email}: off the allowlist, role cleared")


if __name__ == "__main__":
    main()
