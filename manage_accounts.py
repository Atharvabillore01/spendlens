#!/usr/bin/env python3
"""Create and inspect logins from the command line.

The first administrator cannot be created through the API -- `POST /auth/accounts`
requires the `admin` scope, and nobody holds it yet. This is that bootstrap, and
the only path that can mint an admin without already being one.

    python manage_accounts.py list
    python manage_accounts.py create --user-id usr_admin --email admin@acme.com --role admin
    python manage_accounts.py create --user-id usr_a1b2c3d4 --email jose@acme.com
    python manage_accounts.py passwd --user-id usr_admin
    python manage_accounts.py seed              # a login per real user, plus manager + admin
    python manage_accounts.py seed --domain acme.com

The password is read from a prompt, not an argument, so it never lands in shell
history or `ps` output. `--password` exists for scripted setup only.
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.auth.accounts import (  # noqa: E402
    ROLES,
    create_account,
    delete_account,
    list_accounts,
    set_password,
)
from src.auth.principal import AuthError  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.db.engine import get_engine  # noqa: E402
from src.db.schema import create_all  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED = "\033[32m", "\033[31m"


def engine_for(settings):
    engine = get_engine(settings)
    create_all(engine)
    return engine


def ask_password(supplied: str | None) -> str:
    if supplied:
        return supplied
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Confirm: "):
        raise SystemExit(f"{RED}passwords did not match{RESET}")
    return first


def email_for(user_name: str) -> str:
    """`Jose BazBaz` -> `jose.bazbaz`.

    Derived from the name in the spreadsheet so the login belongs to a real
    account holder rather than to a placeholder.
    """
    parts = [re.sub(r"[^a-z0-9]", "", part.lower()) for part in str(user_name).split()]
    return ".".join(p for p in parts if p) or "user"


def seed_data(engine, tenant: str, settings, path: str | None) -> int:
    """Load the real spreadsheet into the transaction store.

    The DataFrame backend reads the file directly, so this only matters for
    STORAGE_BACKEND=sql -- where the database starts empty and a login would
    otherwise point at a user with no transactions. Ingestion is idempotent:
    rows are keyed by a hash of their business fields, so running this twice
    inserts nothing the second time rather than duplicating the dataset.
    """
    from src.ingest.loader import ingest_frame, read_table  # noqa: PLC0415

    source = Path(path) if path else Path(settings.data_path)
    if not source.exists():
        print(f"{RED}no such file:{RESET} {source}")
        return 1

    frame = read_table(source)
    print(f"{DIM}loading {len(frame):,} rows from {source.name}{RESET}")
    report = ingest_frame(
        engine,
        tenant,
        frame,
        filename=source.name,
        chunk_size=settings.ingest_chunk_size,
        max_rows=settings.ingest_max_rows,
    )
    d = report.as_dict()
    print(
        f"  {GREEN}inserted{RESET} {d['inserted']:,}"
        f"  {DIM}duplicates skipped {d['skipped_duplicates']:,}"
        f"  rejected {d['rejected_rows']:,}  users {d['users_seen']}{RESET}"
    )
    for note in d["rejections"][:5]:
        print(f"    {DIM}{note}{RESET}")
    if d["inserted"] == 0 and d["skipped_duplicates"]:
        print(f"  {DIM}already loaded — ingestion is idempotent{RESET}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", default=None, help="defaults to DEFAULT_TENANT_ID")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show every login in the tenant")

    create = sub.add_parser("create", help="grant a login")
    create.add_argument("--user-id", required=True, help="must match the user_id in the transaction data")
    create.add_argument("--email", required=True)
    create.add_argument("--role", default="user", choices=list(ROLES))
    create.add_argument("--password", help="scripted setup only; omit to be prompted")

    passwd = sub.add_parser("passwd", help="reset a password")
    passwd.add_argument("--user-id", required=True)
    passwd.add_argument("--password")

    revoke = sub.add_parser("delete", help="revoke a login (transactions are untouched)")
    revoke.add_argument("--user-id", required=True)

    seed = sub.add_parser("seed", help="load the spreadsheet + a login per real user, plus manager and admin")
    seed.add_argument("--password", default="Ledger-2026", help="shared starting password")
    seed.add_argument("--domain", default="ledger.app", help="email domain for generated logins")
    seed.add_argument("--accounts-only", action="store_true", help="skip loading transactions")
    seed.add_argument(
        "--reset-passwords",
        action="store_true",
        help="realign existing logins to --password (keeps the sign-in page truthful)",
    )

    data = sub.add_parser("seed-data", help="load the real spreadsheet into the SQL store")
    data.add_argument("--file", default=None, help="defaults to DATA_PATH from settings")

    args = parser.parse_args()
    settings = get_settings()
    tenant = args.tenant or settings.default_tenant_id
    engine = engine_for(settings)

    try:
        if args.command == "list":
            accounts = list_accounts(engine, tenant)
            if not accounts:
                print(f"{DIM}no logins yet — run `create` or `seed-demo`{RESET}")
                return 0
            print(f"\n{BOLD}{tenant}{RESET}")
            for a in accounts:
                state = "" if a.is_active else f" {RED}(disabled){RESET}"
                print(f"  {a.email:<28} {a.role:<8} {DIM}{a.user_id}{RESET}{state}")
            return 0

        if args.command == "create":
            account = create_account(
                engine, tenant, args.user_id, args.email, ask_password(args.password), args.role
            )
            print(f"{GREEN}created{RESET} {account.email} ({account.role}) scopes={account.scopes}")
            return 0

        if args.command == "passwd":
            set_password(engine, tenant, args.user_id, ask_password(args.password))
            print(f"{GREEN}password updated{RESET} for {args.user_id}")
            return 0

        if args.command == "delete":
            gone = delete_account(engine, tenant, args.user_id)
            print(f"{GREEN}revoked{RESET} {args.user_id}" if gone else f"{DIM}no login for {args.user_id}{RESET}")
            return 0

        if args.command == "seed-data":
            return seed_data(engine, tenant, settings, args.file)

        if args.command == "seed":
            from src.pipeline import load_transactions  # noqa: PLC0415

            if not args.accounts_only:
                seed_data(engine, tenant, settings, None)
                print()

            frame = load_transactions()
            pairs = frame[["user_id", "user_name"]].drop_duplicates().values.tolist()

            made = 0
            for user_id, user_name in pairs:
                # firstname.lastname@domain, derived from the name in the data
                # rather than invented -- these are the real account holders.
                email = f"{email_for(str(user_name))}@{args.domain}"
                try:
                    create_account(engine, tenant, str(user_id), email, args.password, "user")
                    print(f"  {GREEN}+{RESET} {email:<24} user     {DIM}{user_id}{RESET}")
                    made += 1
                except AuthError as exc:
                    if args.reset_passwords:
                        # The sign-in page advertises one password. An account
                        # that does not use it is a credential that fails when
                        # clicked, which is worse than not listing it at all.
                        set_password(engine, tenant, str(user_id), args.password)
                        print(f"  {GREEN}~{RESET} {email:<24} password realigned {DIM}{user_id}{RESET}")
                    else:
                        print(f"  {DIM}· {email:<24} {exc.message}{RESET}")

            # The manager and admin are staff, not account holders: they have no
            # transactions of their own and exist only to oversee the people who do.
            for email, role, uid in (
                (f"manager@{args.domain}", "manager", "usr_manager"),
                (f"admin@{args.domain}", "admin", "usr_admin"),
            ):
                try:
                    create_account(engine, tenant, uid, email, args.password, role)
                    print(f"  {GREEN}+{RESET} {email:<24} {role:<8} {DIM}{uid}{RESET}")
                    made += 1
                except AuthError as exc:
                    if args.reset_passwords:
                        set_password(engine, tenant, uid, args.password)
                        print(f"  {GREEN}~{RESET} {email:<24} password realigned {DIM}{uid}{RESET}")
                    else:
                        print(f"  {DIM}· {email:<24} {exc.message}{RESET}")

            print(f"\n{made} login(s) created · password {BOLD}{args.password}{RESET}")
            print(f"{DIM}manager and admin hold no transactions of their own; they read{RESET}")
            print(f"{DIM}account holders by naming one, which their scope permits{RESET}")
            return 0

    except AuthError as exc:
        print(f"{RED}{exc.code}{RESET}: {exc.message}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
