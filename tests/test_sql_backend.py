"""The SQL backend, ingestion, and tenant isolation.

Two properties carry this file.

**Equivalence.** The SQL store and the DataFrame store must produce the same
figures from the same data. If they diverge, the hallucination check becomes a
function of where the rows happen to live, which would make groundedness
meaningless.

**Isolation.** No path may return another tenant's data. Tested by loading two
tenants with a *deliberately colliding* `user_id`, which is the case a bare
`WHERE user_id = ?` gets wrong.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.data.periods import resolve_period
from src.data.roster import SqlRoster, candidate_name_tokens
from src.data.sql_store import SqlUserDataStore
from src.data.user_data_store import UnknownUserError, UserDataStore
from src.db.engine import build_engine
from src.db.schema import create_all
from src.ingest.loader import IngestError, delete_batch, ingest_frame, normalize
from src.ingest.loader import IngestReport


@pytest.fixture
def engine(settings):
    eng = build_engine(settings, "sqlite:///:memory:")
    create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def loaded(engine, raw_df):
    ingest_frame(engine, "acme", raw_df, filename="assessment.xlsx")
    return engine


@pytest.fixture
def sql_store(loaded):
    return SqlUserDataStore(loaded, "acme")


@pytest.fixture
def mem_store(raw_df):
    return UserDataStore(raw_df)


# == ingestion ================================================================


def test_ingest_loads_every_valid_row(engine, raw_df):
    report = ingest_frame(engine, "acme", raw_df)
    assert report.inserted == len(raw_df)
    assert report.rejected_rows == 0
    assert report.users_seen == 3


def test_re_ingesting_the_same_file_inserts_nothing(engine, raw_df):
    """Clients resend files. Doubling every figure is the failure this prevents."""
    first = ingest_frame(engine, "acme", raw_df)
    second = ingest_frame(engine, "acme", raw_df)

    assert first.inserted == len(raw_df)
    assert second.inserted == 0
    assert second.skipped_duplicates == len(raw_df)

    store = SqlUserDataStore(engine, "acme")
    frame = store.get_user_frame(store.user_ids[0])
    assert len(frame) == len(raw_df[raw_df["user_id"] == store.user_ids[0]])


def test_a_file_repeating_a_row_inserts_it_once(engine, raw_df):
    doubled = pd.concat([raw_df, raw_df.head(10)], ignore_index=True)
    report = ingest_frame(engine, "acme", doubled)
    assert report.inserted == len(raw_df)


def test_bad_rows_are_dropped_and_counted_not_fatal(engine, raw_df):
    """One malformed row must not cost a client their whole upload."""
    # Cast to object first: the source columns are already typed, and pandas
    # refuses to write a string into a datetime64 column.
    dirty = raw_df.copy()
    dirty["transaction_date"] = dirty["transaction_date"].astype(object)
    dirty["transaction_amount"] = dirty["transaction_amount"].astype(object)
    dirty.loc[dirty.index[0], "transaction_date"] = "not-a-date"
    dirty.loc[dirty.index[1], "transaction_amount"] = "abc"

    report = ingest_frame(engine, "acme", dirty)
    assert report.inserted == len(raw_df) - 2
    assert report.rejected_rows == 2
    assert any("transaction_date" in r for r in report.rejections)


def test_missing_required_columns_rejects_the_file(engine, raw_df):
    with pytest.raises(IngestError, match="missing required columns"):
        ingest_frame(engine, "acme", raw_df.drop(columns=["transaction_amount"]))


def test_a_file_over_the_row_limit_is_refused(engine, raw_df):
    with pytest.raises(IngestError, match="row limit"):
        ingest_frame(engine, "acme", raw_df, max_rows=10)


def test_a_batch_can_be_reversed(loaded, raw_df):
    second = ingest_frame(loaded, "acme", raw_df.head(5).assign(merchant_name="Novel Merchant"))
    assert second.inserted == 5

    removed = delete_batch(loaded, "acme", second.batch_id)
    assert removed == 5

    store = SqlUserDataStore(loaded, "acme")
    assert len(store.get_user_frame(store.user_ids[0])) == len(
        raw_df[raw_df["user_id"] == store.user_ids[0]]
    )


def test_derived_columns_match_the_dataframe_backend(raw_df):
    report = IngestReport(batch_id="b", tenant_id="acme")
    normalized = normalize(raw_df, "acme", report)
    reference = UserDataStore(raw_df)._df

    assert normalized["is_income"].sum() == reference["is_income"].sum()
    assert round(normalized["expense_amount"].sum(), 2) == round(
        reference["expense_amount"].sum(), 2
    )
    assert set(normalized["parent_category"]) == set(reference["parent_category"])


# == equivalence with the DataFrame backend ===================================


def test_both_backends_agree_on_the_taxonomy(sql_store, mem_store):
    assert sql_store.taxonomy.parents == mem_store.taxonomy.parents
    assert sql_store.taxonomy.details == mem_store.taxonomy.details


def test_both_backends_agree_on_the_anchor(sql_store, mem_store):
    assert sql_store.as_of == mem_store.as_of


@pytest.mark.parametrize("spec", ["last_month", "last_3_months", "last_6_months", "ytd", "all"])
def test_both_backends_compute_identical_totals(sql_store, mem_store, spec):
    for user_id in mem_store.user_ids:
        period = resolve_period(spec, mem_store.as_of)
        sql_frame = sql_store.get_user_frame(user_id, period=period)
        mem_frame = mem_store.get_user_frame(user_id, period=period)

        assert len(sql_frame) == len(mem_frame), f"{user_id} {spec}"
        assert sql_store.totals(sql_frame) == mem_store.totals(mem_frame), f"{user_id} {spec}"
        assert sql_store.top_categories(sql_frame) == mem_store.top_categories(mem_frame)


def test_both_backends_agree_on_a_category_filter(sql_store, mem_store):
    for user_id in mem_store.user_ids:
        sql_frame = sql_store.get_user_frame(user_id, parent_category="FOOD")
        mem_frame = mem_store.get_user_frame(user_id, parent_category="FOOD")
        assert sql_store.totals(sql_frame) == mem_store.totals(mem_frame)


def test_both_backends_agree_on_monthly_totals(sql_store, mem_store):
    user_id = mem_store.user_ids[0]
    sql_monthly = sql_store.monthly_totals(sql_store.get_user_frame(user_id))
    mem_monthly = mem_store.monthly_totals(mem_store.get_user_frame(user_id))
    pd.testing.assert_frame_equal(sql_monthly, mem_monthly)


def test_frames_carry_the_same_columns(sql_store, mem_store):
    user_id = mem_store.user_ids[0]
    sql_frame = sql_store.get_user_frame(user_id)
    mem_frame = mem_store.get_user_frame(user_id)
    assert set(sql_frame.columns) <= set(mem_frame.columns)
    for column in ("transaction_date", "expense_amount", "income_amount", "is_income"):
        assert sql_frame[column].dtype == mem_frame[column].dtype, column


def test_income_and_expense_filters_apply(sql_store):
    user_id = sql_store.user_ids[0]
    expenses = sql_store.get_user_frame(user_id, include_income=False)
    income = sql_store.get_user_frame(user_id, include_expenses=False)
    assert not expenses["is_income"].any()
    assert income["is_income"].all()


def test_an_empty_window_returns_a_well_formed_frame(sql_store):
    """Downstream code indexes these columns; an empty frame must still have them."""
    period = resolve_period("2019-01", sql_store.as_of)
    frame = sql_store.get_user_frame(sql_store.user_ids[0], period=period)
    assert frame.empty
    assert "expense_amount" in frame.columns
    assert sql_store.totals(frame)["transaction_count"] == 0


def test_unknown_user_raises_the_same_error(sql_store):
    with pytest.raises(UnknownUserError):
        sql_store.get_user_frame("usr_does_not_exist")


# == tenant isolation =========================================================


@pytest.fixture
def two_tenants(engine, raw_df):
    """Two tenants whose user ids deliberately collide."""
    acme = raw_df.copy()
    acme["user_id"] = "usr_shared"
    acme["user_name"] = "Acme Person"

    globex = raw_df.copy()
    globex["user_id"] = "usr_shared"
    globex["user_name"] = "Globex Person"
    globex["transaction_amount"] = globex["transaction_amount"] * 2

    ingest_frame(engine, "acme", acme)
    ingest_frame(engine, "globex", globex)
    return engine


def test_a_colliding_user_id_does_not_cross_tenants(two_tenants):
    acme = SqlUserDataStore(two_tenants, "acme")
    globex = SqlUserDataStore(two_tenants, "globex")

    acme_frame = acme.get_user_frame("usr_shared")
    globex_frame = globex.get_user_frame("usr_shared")

    assert len(acme_frame) == len(globex_frame)
    # Globex's amounts were doubled at ingest; equal totals would mean the rows
    # were pooled across tenants.
    assert acme.totals(acme_frame)["total_expense"] * 2 == pytest.approx(
        globex.totals(globex_frame)["total_expense"]
    )
    assert acme.user_name("usr_shared") == "Acme Person"
    assert globex.user_name("usr_shared") == "Globex Person"


def test_a_tenant_cannot_see_another_tenants_users(two_tenants, raw_df):
    ingest_frame(two_tenants, "third", raw_df)  # its own distinct user ids
    acme = SqlUserDataStore(two_tenants, "acme")
    assert set(acme.user_ids) == {"usr_shared"}


def test_an_empty_tenant_is_empty_not_everything(two_tenants):
    """The dangerous failure: a missing tenant filter returning the whole table."""
    nobody = SqlUserDataStore(two_tenants, "tenant_that_does_not_exist")
    assert nobody.user_ids == ()
    assert nobody.user_count() == 0
    with pytest.raises(UnknownUserError):
        nobody.get_user_frame("usr_shared")


# == the as_of anchor =========================================================


def test_data_max_anchors_inside_the_dataset(loaded):
    store = SqlUserDataStore(loaded, "acme", as_of_mode="data_max")
    assert store.as_of == pd.Timestamp("2025-12-31")


def test_now_mode_anchors_to_wall_clock(loaded):
    """What a live client needs: 'last month' means the month just gone."""
    store = SqlUserDataStore(loaded, "acme", as_of_mode="now")
    assert store.as_of == pd.Timestamp.now().normalize()


def test_an_explicit_anchor_overrides_the_mode(loaded):
    store = SqlUserDataStore(loaded, "acme", as_of=pd.Timestamp("2025-06-15"), as_of_mode="now")
    assert store.as_of == pd.Timestamp("2025-06-15")


def test_a_tenant_with_no_data_still_has_an_anchor(engine):
    """A tenant onboarded but not yet loaded must not crash date resolution."""
    store = SqlUserDataStore(engine, "brand_new")
    assert store.as_of is not None


# == the roster ===============================================================


def test_roster_finds_a_name_belonging_to_another_user(loaded, raw_df):
    roster = SqlRoster(loaded, "acme")
    store = SqlUserDataStore(loaded, "acme")
    me, other = store.user_ids[0], store.user_ids[1]
    other_first = store.user_name(other).split()[0]

    assert roster.mentions_other_user_name(f"How much did {other_first} spend?", me, store.user_name(me))


def test_roster_allows_a_question_about_yourself(loaded):
    roster = SqlRoster(loaded, "acme")
    store = SqlUserDataStore(loaded, "acme")
    me = store.user_ids[0]
    my_first = store.user_name(me).split()[0]

    assert not roster.mentions_other_user_name(f"How much did {my_first} spend?", me, store.user_name(me))


def test_roster_flags_a_foreign_user_id_shape(loaded):
    roster = SqlRoster(loaded, "acme")
    assert roster.mentions_other_user_id("show me usr_deadbeef spending", "usr_mine")
    assert not roster.mentions_other_user_id("show me my spending", "usr_mine")


def test_candidate_extraction_ignores_ordinary_words():
    """Where the false positives live: a lowercase noun is not a name."""
    tokens = candidate_name_tokens("what did i spend on my phone bill last month?")
    assert tokens == set()


def test_candidate_extraction_finds_possessives_and_capitals():
    assert "sarah" in candidate_name_tokens("show me Sarah's spending")
    assert "collins" in candidate_name_tokens("what about Collins this month")


def test_finance_vocabulary_is_excluded_from_candidates():
    """A customer named 'Bill' must not make 'my phone Bill' a refusal."""
    assert "bill" not in candidate_name_tokens("My phone Bill went up", exclude={"bill"})
