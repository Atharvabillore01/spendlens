"""Data layer: taxonomy derivation, per-user isolation, period resolution."""

from __future__ import annotations

import pandas as pd
import pytest

from src.data.category_taxonomy import CategoryTaxonomy
from src.data.periods import Period, month_name, normalize_spec, resolve_period
from src.data.profile_builder import ProfileBuilder
from src.data.user_data_store import UnknownUserError, UserDataStore

AS_OF = pd.Timestamp("2025-12-31")


# -- CategoryTaxonomy ---------------------------------------------------------


def test_split_uses_last_underscore():
    assert CategoryTaxonomy.split("RENT_HOUSING") == ("RENT", "HOUSING")
    assert CategoryTaxonomy.split("FASTFOOD_FOOD") == ("FASTFOOD", "FOOD")
    # Multi-underscore is unambiguous because only the last one is the boundary.
    assert CategoryTaxonomy.split("DRY_CLEANING_SHOPPING") == ("DRY_CLEANING", "SHOPPING")


def test_split_tolerates_missing_separator():
    assert CategoryTaxonomy.split("MISC") == ("UNSPECIFIED", "MISC")


def test_vocabulary_is_derived_from_data_not_hardcoded(raw_df):
    taxonomy = CategoryTaxonomy.from_frame(raw_df)
    assert "HOUSING" in taxonomy.parents and "INCOME" in taxonomy.parents
    assert "INCOME" not in taxonomy.spend_parents
    # A brand-new category needs no code change to be picked up.
    extended = CategoryTaxonomy(list(taxonomy.details) + ["SOLAR_UTILITIESNEW"])
    assert "UTILITIESNEW" in extended.parents


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("FOOD", "FOOD"), ("food", "FOOD"), ("Food", "FOOD"),
        ("groceries", "FOOD"), ("GROCERIES", "FOOD"), ("dining", "FOOD"),
        ("food spending", "FOOD"), ("rent", "HOUSING"), ("transportation", "TRANSPORT"),
        ("null", None), ("", None), (None, None), ("gibberish", None),
    ],
)
def test_normalize_parent(raw_df, raw, expected):
    assert CategoryTaxonomy.from_frame(raw_df).normalize_parent(raw) == expected


def test_rollup_buckets_the_tail_as_other(raw_df):
    taxonomy = CategoryTaxonomy.from_frame(raw_df)
    frame = taxonomy.annotate(raw_df)
    frame["expense_amount"] = frame["transaction_amount"].clip(lower=0)
    rolled = taxonomy.rollup(frame, top_n=3, value_column="expense_amount")
    assert len(rolled) == 4 and rolled.iloc[-1]["category"] == "Other"
    assert rolled["amount"].is_monotonic_decreasing or rolled.iloc[0]["amount"] >= rolled.iloc[1]["amount"]


def test_rollup_of_empty_frame_is_empty(raw_df):
    taxonomy = CategoryTaxonomy.from_frame(raw_df)
    assert taxonomy.rollup(raw_df.iloc[0:0], top_n=5).empty


# -- UserDataStore ------------------------------------------------------------


def test_store_derives_columns_and_sign_convention(store):
    frame = store.get_user_frame(store.user_ids[0])
    assert {"parent_category", "subcategory", "is_income", "expense_amount", "income_amount"} <= set(frame.columns)
    # Verified property of this dataset: income rows are exactly the negative ones.
    assert (frame.loc[frame["is_income"], "transaction_amount"] < 0).all()
    assert (frame.loc[~frame["is_income"], "expense_amount"] >= 0).all()


def test_get_user_frame_never_leaks_other_users(store):
    for user_id in store.user_ids:
        frame = store.get_user_frame(user_id)
        assert frame["user_id"].nunique() == 1
        assert frame["user_id"].iloc[0] == user_id


def test_get_user_frame_returns_a_copy(store):
    user_id = store.user_ids[0]
    frame = store.get_user_frame(user_id)
    frame.loc[frame.index[0], "transaction_amount"] = 999_999
    assert store.get_user_frame(user_id)["transaction_amount"].iloc[0] != 999_999


def test_unknown_user_raises_at_the_boundary(store):
    assert store.validate_user("usr_does_not_exist") is False
    with pytest.raises(UnknownUserError):
        store.get_user_frame("usr_does_not_exist")


def test_as_of_defaults_to_last_transaction(store):
    assert store.as_of == pd.Timestamp("2025-12-31")


def test_as_of_is_overridable(raw_df):
    assert UserDataStore(raw_df, as_of=pd.Timestamp("2025-09-15")).as_of == pd.Timestamp("2025-09-15")


def test_filters_compose(store):
    user_id = store.user_ids[0]
    period = resolve_period("last_month", store.as_of)
    frame = store.get_user_frame(user_id, period=period, parent_category="HOUSING", include_income=False)
    assert (frame["parent_category"] == "HOUSING").all()
    assert (~frame["is_income"]).all()
    assert frame["transaction_date"].between(period.start, period.end).all()


def test_monthly_totals_shape(store):
    monthly = store.monthly_totals(store.get_user_frame(store.user_ids[0]))
    assert list(monthly.columns) == ["month", "expense", "income", "net"]
    assert (monthly["net"] == monthly["income"] - monthly["expense"]).all()


def test_top_categories_excludes_income(store):
    frame = store.get_user_frame(store.user_ids[0])
    assert "INCOME" not in [name for name, _ in store.top_categories(frame, n=20)]


# -- ProfileBuilder -----------------------------------------------------------


def test_profile_contains_required_fields(store):
    profile = ProfileBuilder(store).build(store.user_ids[0])
    for key in ("user_name", "date_range", "top_categories", "avg_monthly_spend", "avg_monthly_income"):
        assert key in profile
    assert profile["avg_monthly_spend"] > 0


def test_profile_summary_is_prompt_sized(store):
    profile = ProfileBuilder(store).build(store.user_ids[0])
    summary = ProfileBuilder.summarize_for_prompt(profile)
    assert profile["user_name"] in summary and len(summary) < 600


# -- periods ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("last month", "last_month"), ("Last Month", "last_month"), ("LAST_MONTH", "last_month"),
        ("previous month", "last_month"), ("the last 3 months", "last_3_months"),
        ("last-6-months", "last_6_months"), ("year to date", "ytd"), ("all time", "all"),
        (None, "last_3_months"), ("", "last_3_months"),
    ],
)
def test_normalize_spec_absorbs_llm_freeform(raw, expected):
    assert normalize_spec(raw) == expected


def test_last_month_is_the_previous_calendar_month():
    period = resolve_period("last_month", AS_OF)
    assert (period.start, period.end) == (pd.Timestamp("2025-11-01"), pd.Timestamp("2025-11-30"))
    assert month_name(period) == "November 2025"


def test_this_month_is_clipped_to_as_of():
    period = resolve_period("this_month", pd.Timestamp("2025-12-15"))
    assert (period.start, period.end) == (pd.Timestamp("2025-12-01"), pd.Timestamp("2025-12-15"))


def test_last_n_months_includes_the_current_month():
    period = resolve_period("last_3_months", AS_OF)
    assert (period.start, period.end) == (pd.Timestamp("2025-10-01"), pd.Timestamp("2025-12-31"))


def test_months_integer_is_equivalent_to_last_n_months():
    assert resolve_period(None, AS_OF, months=6).start == resolve_period("last_6_months", AS_OF).start


def test_explicit_calendar_month():
    period = resolve_period("2025-07", AS_OF)
    assert (period.start, period.end) == (pd.Timestamp("2025-07-01"), pd.Timestamp("2025-07-31"))


def test_all_is_unbounded():
    assert resolve_period("all", AS_OF).is_unbounded


def test_unrecognized_spec_falls_back_instead_of_raising():
    assert resolve_period("whenever the moon is full", AS_OF).spec == "last_3_months"


def test_period_mask_is_inclusive():
    period = Period(pd.Timestamp("2025-11-01"), pd.Timestamp("2025-11-30"), "2025-11", "last_month")
    dates = pd.Series(pd.to_datetime(["2025-10-31", "2025-11-01", "2025-11-30", "2025-12-01"]))
    assert period.mask(dates).tolist() == [False, True, True, False]


def test_relative_dates_land_inside_the_dataset(store):
    """The bug this whole module exists to prevent: 'last month' must not be empty."""
    for user_id in store.user_ids:
        frame = store.get_user_frame(user_id, period=resolve_period("last_month", store.as_of))
        assert not frame.empty
