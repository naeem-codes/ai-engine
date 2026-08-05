r"""Create (or update) the Supabase Auth user that represents one client. DEV TOOL.

Why a user rather than a hand-minted token: this project signs JWTs with an ASYMMETRIC key
(ES256), whose private half is not exportable, so only Supabase can issue a token it will
accept. The engine therefore signs in as a per-client user and uses the token Supabase hands
back. The RLS policy is unchanged — it reads `app_metadata.client_id`, which is stamped onto
the user here, and Supabase copies into every access token it issues for them.

Consequences worth knowing:
  * Revoking a client = disabling this ONE user (Dashboard → Authentication → Users → ban),
    which is far better than the minted-token model where revocation meant rotating the
    project secret and invalidating every client at once.
  * `app_metadata` is deliberate: unlike `user_metadata` it cannot be edited by the user
    themselves, so a client cannot rewrite their own client_id and read someone else's rules.

Needs the SERVICE ROLE / secret key. That key stays on your machine and is NEVER shipped —
it bypasses RLS entirely.

Usage:
    $env:SUPABASE_URL="https://<ref>.supabase.co"
    $env:SUPABASE_SERVICE_KEY="<service role / secret key>"
    python tools/create_client_user.py --client-id lumi
    python tools/create_client_user.py --client-id lumi --email lumi@yourdomain.com
    python tools/create_client_user.py --client-id lumi --password "..."      # else generated

Prints the email/password to put in the client's config (or into build-release.ps1's
-ConfigFile). Store them in your password manager; they are the client's credentials.
"""

import argparse
import json
import os
import secrets
import string
import sys

import httpx

TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def _admin_headers(service_key: str) -> dict[str, str]:
    return {"apikey": service_key, "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json"}


def generate_password(length: int = 28) -> str:
    """Long random password — nobody types this, so make it strong."""
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def find_user(url: str, service_key: str, email: str) -> dict | None:
    """Existing user with this email, or None. Makes the script safe to re-run."""
    r = httpx.get(f"{url}/auth/v1/admin/users", headers=_admin_headers(service_key),
                  params={"filter": email, "per_page": "50"}, timeout=TIMEOUT)
    r.raise_for_status()
    for user in r.json().get("users", []):
        if (user.get("email") or "").lower() == email.lower():
            return user
    return None


def create_or_update(url: str, service_key: str, email: str, password: str,
                     client_id: str) -> tuple[dict, bool]:
    """Returns (user, created). Idempotent: re-running updates the claim and password."""
    existing = find_user(url, service_key, email)
    payload = {
        "email": email,
        "password": password,
        # No inbox exists for these addresses, and the engine signs in by password, so the
        # user must be confirmed outright or every sign-in fails with "email not confirmed".
        "email_confirm": True,
        "app_metadata": {"client_id": client_id},
    }
    if existing:
        r = httpx.put(f"{url}/auth/v1/admin/users/{existing['id']}",
                      headers=_admin_headers(service_key), json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json(), False
    r = httpx.post(f"{url}/auth/v1/admin/users", headers=_admin_headers(service_key),
                   json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json(), True


def verify_sign_in(url: str, anon_key: str, email: str, password: str) -> dict:
    """Prove the credentials work and the claim lands in the issued token."""
    r = httpx.post(f"{url}/auth/v1/token", params={"grant_type": "password"},
                   headers={"apikey": anon_key, "Content-Type": "application/json"},
                   json={"email": email, "password": password}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def claim_of(access_token: str) -> str:
    import base64
    payload = access_token.split(".")[1]
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return (data.get("app_metadata") or {}).get("client_id", "")


def main() -> int:
    ap = argparse.ArgumentParser(description="Create/update a client's Supabase Auth user.")
    ap.add_argument("--client-id", required=True, help="tenant id stamped into app_metadata")
    ap.add_argument("--email", default="", help="defaults to <client-id>@lumi-clients.local")
    ap.add_argument("--password", default="", help="defaults to a generated 28-char password")
    ap.add_argument("--url", default=os.getenv("SUPABASE_URL", ""))
    ap.add_argument("--service-key", default=os.getenv("SUPABASE_SERVICE_KEY", ""))
    ap.add_argument("--anon-key", default=os.getenv("SUPABASE_KEY", ""),
                    help="used only to verify sign-in afterwards")
    args = ap.parse_args()

    url = args.url.rstrip("/")
    if not url or not args.service_key:
        print("error: --url and --service-key (or SUPABASE_URL / SUPABASE_SERVICE_KEY) required",
              file=sys.stderr)
        return 2

    email = args.email or f"{args.client_id}@lumi-clients.local"
    password = args.password or generate_password()

    try:
        user, created = create_or_update(url, args.service_key, email, password, args.client_id)
    except httpx.HTTPStatusError as exc:
        print(f"error: admin API returned {exc.response.status_code}: {exc.response.text[:300]}",
              file=sys.stderr)
        return 1

    print()
    print(f"{'created' if created else 'updated'} user : {email}")
    print(f"user id        : {user.get('id')}")
    print(f"app_metadata   : {user.get('app_metadata')}")

    if args.anon_key:
        try:
            session = verify_sign_in(url, args.anon_key, email, password)
            token_claim = claim_of(session["access_token"])
            print(f"sign-in check  : OK (token carries client_id={token_claim!r}, "
                  f"expires in {session.get('expires_in')}s)")
            if token_claim != args.client_id:
                print("WARNING: the token's client_id does not match — RLS will deny access.")
        except (httpx.HTTPStatusError, KeyError) as exc:
            print(f"sign-in check  : FAILED — {exc}")
    else:
        print("sign-in check  : skipped (no --anon-key/SUPABASE_KEY given)")

    print()
    print("Put these in the engine config (.env for local testing, or build-release.ps1")
    print("-ConfigFile for a client build):")
    print()
    print(f"  SUPABASE_EMAIL={email}")
    print(f"  SUPABASE_PASSWORD={password}")
    print()
    print("Keep a copy in your password manager — the password is not recoverable from")
    print("Supabase, though you can re-run this script to set a new one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
