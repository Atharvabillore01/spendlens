"""Physical schema for the multi-tenant transaction store.

Two decisions here carry most of the weight.

**`tenant_id` is the first column of every primary key and every index.** Not a
filter applied later -- the leading edge of the index. A query that forgets the
tenant cannot accidentally hit another tenant's partition efficiently, and the
one that matters (`tenant_id, user_id, transaction_date`) covers the only access
pattern the pipeline has: one user's rows inside one date window.

**The derived columns are stored, not computed at read time.** `subcategory`,
`parent_category`, `is_income`, `expense_amount` and `income_amount` are all
functions of the two raw columns, so writing them looks redundant. It isn't:
computing them per query means every read re-derives 100M rows' worth of string
splits, and it puts the category filter out of reach of an index. They are
written once at ingest, by the same `CategoryTaxonomy` the DataFrame path uses,
so the two backends cannot drift.

Portability: SQLAlchemy Core rather than an ORM, so the same statements run on
Postgres (production) and SQLite (tests, local runs) without a translation
layer. Postgres-only refinements live in `postgres_tuning()`.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

metadata = MetaData()


# A client organisation. One row per company using the product.
tenants = Table(
    "tenants",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("name", String(255), nullable=False),
    # Per-tenant override of Settings.as_of_mode. A tenant uploading a frozen
    # historical extract needs "data_max"; one streaming live transactions needs
    # "now". Getting this wrong silently shifts what "last month" means.
    Column("as_of_mode", String(16), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("is_active", Boolean, nullable=False, default=True),
)


# The people whose transactions we hold. Distinct from whoever authenticates:
# a support agent may read a user's data, but the data belongs to the user.
app_users = Table(
    "app_users",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("user_id", String(64), primary_key=True),
    Column("user_name", String(255), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Index("ix_app_users_tenant_name", "tenant_id", "user_name"),
)


# Login credentials and role. Deliberately separate from `app_users`:
#
# `app_users` is written by ingest -- a row appears there because a transaction
# named that person, whether or not they can log in. Credentials are granted by
# an administrator, to a subset of those people, and carry a secret. Mixing the
# two would mean an upload could create or disturb a login, which is exactly the
# coupling you do not want between a data path and an auth path.
#
# `role` is the durable fact; scopes are derived from it at token-mint time
# (see `src/auth/accounts.py`), so re-scoping a role does not require reissuing
# rows here.
credentials = Table(
    "credentials",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("user_id", String(64), primary_key=True),
    Column("email", String(255), nullable=False),
    # scrypt, stored as an opaque "scrypt$n$r$p$salt$hash" string. Never a
    # reversible encoding, and never logged.
    Column("password_hash", String(512), nullable=False),
    Column("role", String(32), nullable=False, server_default="user"),
    Column("is_active", Boolean, nullable=False, server_default="1"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("last_login_at", DateTime(timezone=True), nullable=True),
    # Email is the login handle, unique within a tenant -- two tenants may
    # legitimately have the same person.
    UniqueConstraint("tenant_id", "email", name="uq_credentials_tenant_email"),
    Index("ix_credentials_login", "tenant_id", "email"),
)


# One row per (user, word-in-their-name), written at ingest.
#
# This exists so the cross-user guardrail stays an indexed lookup. Loading every
# name into the process to scan a prompt is fine for 3 users and untenable for
# 50,000 -- and matching a prompt against 50,000 first names would flag half of
# them spuriously. Here the prompt supplies a handful of candidate tokens and
# the database answers whether any belongs to someone other than the caller.
user_name_parts = Table(
    "user_name_parts",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("part", String(128), primary_key=True),
    Column("user_id", String(64), primary_key=True),
    Index("ix_name_parts_lookup", "tenant_id", "part"),
)


# A question a user sent to their manager.
#
# Deliberately *not* modelled as a chat thread: the shape here is one question,
# optionally one machine-computed answer, and one human reply. Threading invites
# an unbounded message table and a read/unread model, neither of which the
# product needs to do the job -- get a person an answer about their own money.
#
# `computed_answer` is what the pipeline produced when the manager ran the
# question against the asker's data. It is kept separate from `reply` so the
# audit trail distinguishes "the system said this" from "a person wrote this",
# which matters when the reply contradicts the figures.
manager_requests = Table(
    "manager_requests",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("request_id", String(32), primary_key=True),
    Column("from_user_id", String(64), nullable=False),
    Column("from_user_name", String(255), nullable=False, server_default=""),
    Column("question", Text, nullable=False),
    # open -> answered -> closed. Never deleted: an answered question about
    # somebody's finances is a record.
    Column("status", String(16), nullable=False, server_default="open"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("computed_answer", Text, nullable=True),
    Column("computed_summary", Text, nullable=True),   # JSON blob, for the chart
    Column("computed_at", DateTime(timezone=True), nullable=True),
    Column("reply", Text, nullable=True),
    Column("replied_by", String(64), nullable=True),
    Column("replied_at", DateTime(timezone=True), nullable=True),
    Index("ix_requests_inbox", "tenant_id", "status", "created_at"),
    Index("ix_requests_mine", "tenant_id", "from_user_id", "created_at"),
)


transactions = Table(
    "transactions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant_id", String(64), nullable=False),
    Column("user_id", String(64), nullable=False),
    Column("transaction_date", DateTime, nullable=False),
    Column("transaction_amount", Float, nullable=False),
    Column("transaction_category_detail", String(128), nullable=False),
    Column("merchant_name", String(255), nullable=False, default=""),
    # -- derived at ingest (see module docstring) -----------------------------
    Column("subcategory", String(128), nullable=False),
    Column("parent_category", String(128), nullable=False),
    Column("is_income", Boolean, nullable=False),
    Column("expense_amount", Float, nullable=False),
    Column("income_amount", Float, nullable=False),
    # -- provenance -----------------------------------------------------------
    Column("batch_id", String(64), nullable=True),
    Column("row_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    # The hot path: one user, one window. Also serves the unqualified
    # "everything for this user" scan used to build a profile.
    Index("ix_tx_tenant_user_date", "tenant_id", "user_id", "transaction_date"),
    # Category-filtered windows ("show me my food spending").
    Index("ix_tx_tenant_user_parent", "tenant_id", "user_id", "parent_category"),
    # Re-uploading the same file must not double every figure. The hash covers
    # the natural key (user, date, amount, category, merchant), so an idempotent
    # re-ingest is a no-op while a genuinely new row still lands.
    UniqueConstraint("tenant_id", "row_hash", name="uq_tx_tenant_row"),
)


# One row per upload, so a bad import can be found and reversed.
ingest_batches = Table(
    "ingest_batches",
    metadata,
    Column("batch_id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("filename", String(512), nullable=False, default=""),
    Column("row_count", Integer, nullable=False, default=0),
    Column("inserted_count", Integer, nullable=False, default=0),
    Column("skipped_count", Integer, nullable=False, default=0),
    Column("status", String(32), nullable=False, default="pending"),
    Column("error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Index("ix_batches_tenant_created", "tenant_id", "created_at"),
)


# Shared cache and rate-limit state.
#
# The in-memory backend gives every process its own buckets, which on serverless
# means every invocation starts with a full allowance and the limit never binds
# at all. Redis is the usual answer; this exists because the deployment already
# has a database and adding a second stateful dependency to a demo buys nothing.
# One indexed lookup and one upsert per limited request is well within what this
# workload asks of Postgres.
kv_entries = Table(
    "kv_entries",
    metadata,
    Column("key", String(512), primary_key=True),
    Column("value", Text, nullable=False),
    # Nullable means "never expires". Expiry is enforced on read rather than by
    # a sweeper, so a missed cleanup can never serve a stale value.
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Index("ix_kv_expires", "expires_at"),
)

# Append-only audit trail.
#
# stdout is fine until someone needs to answer "who read this account, and
# when" six months later. Rows are never updated or deleted by the application;
# the columns that get queried are promoted out of the payload so that question
# is an index lookup rather than a scan over JSON.
audit_entries = Table(
    "audit_entries",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("timestamp", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("event", String(64), nullable=False),
    Column("tenant_id", String(64), nullable=True),
    Column("actor_id", String(64), nullable=True),
    Column("user_id", String(64), nullable=True),
    Column("impersonated", Boolean, nullable=False, default=False),
    Column("payload", Text, nullable=False, default="{}"),
    Index("ix_audit_tenant_time", "tenant_id", "timestamp"),
    Index("ix_audit_actor_time", "actor_id", "timestamp"),
    Index("ix_audit_subject_time", "user_id", "timestamp"),
)


POSTGRES_TUNING = (
    # The profile scan reads one user's whole history; BRIN on the date column
    # keeps that cheap once the table is large and naturally date-ordered.
    "CREATE INDEX IF NOT EXISTS ix_tx_date_brin ON transactions USING brin (transaction_date)",
)


def create_all(engine) -> None:
    """Create tables and indexes if absent. Safe to call on every boot."""
    metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            from sqlalchemy import text  # noqa: PLC0415

            for statement in POSTGRES_TUNING:
                conn.execute(text(statement))


__all__ = [
    "metadata",
    "tenants",
    "app_users",
    "user_name_parts",
    "transactions",
    "ingest_batches",
    "kv_entries",
    "audit_entries",
    "create_all",
]
