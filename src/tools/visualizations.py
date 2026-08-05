"""The three tool-callable chart functions.

Each returns a `ChartResult` carrying both the PNG path and the numbers that
produced it. Those numbers are the *only* legitimate source of figures in the
final answer -- the output guardrail cross-checks the LLM's prose against
`ChartResult.grounding` and strips anything it can't match. That is the concrete
mechanism behind "the LLM narrates, Pandas computes".

Design rules applied here (and why):
  * **One y-axis, always.** `plot_income_vs_expense` previously put the net line
    on a twin axis. Two scales on one plot invent a correlation that isn't in the
    data. Income, expense and net are all dollars, so they share one scale.
  * **Selective direct labels.** The peak and the endpoint get a value; the axis
    carries the rest. A number on every point goes unread.
  * **Text never wears the series color.** Marks carry identity; labels use ink
    tokens. The legend swatch is what ties them together.
  * **Hairline solid grid, thin marks, generous air.**

Palettes are validated (OKLab CVD separation, chroma, lightness band, contrast)
against the surface each theme actually renders on -- see README §Charts.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import matplotlib

matplotlib.use("Agg")  # headless: no display server, deterministic output
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from ..data.periods import (  # noqa: E402
    Period,
    month_label,
    month_name,
    preceding_period,
    resolve_period,
)
from ..data.user_data_store import UserDataStore  # noqa: E402

# Categorical slots are assigned in fixed order and never cycled; a 9th category
# folds into "Other" rather than generating a new hue.
THEMES: dict[str, dict[str, Any]] = {
    "light": {
        "surface": "#ffffff",
        "ink": "#0b0b0b",
        "ink_secondary": "#52514e",
        "muted": "#898781",
        "grid": "#e6e6e2",
        "axis": "#c9c9c3",
        "categorical": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                        "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
        "other": "#b0b3b8",
        "income": "#008300",
        "expense": "#e34948",
        "net": "#2a78d6",
    },
    "dark": {
        "surface": "#161a20",
        "ink": "#ffffff",
        "ink_secondary": "#c3c2b7",
        "muted": "#898781",
        "grid": "#262b32",
        "axis": "#383e46",
        "categorical": ["#3987e5", "#d95926", "#199e70", "#c98500",
                        "#d55181", "#008300", "#9085e9", "#e66767"],
        "other": "#6e7781",
        "income": "#008300",
        "expense": "#e66767",
        "net": "#3987e5",
    },
}

BAR_MAX_PX = 24       # bars never fill their slot; the leftover band is air
MARKER_SIZE = 5.5     # >= 8px diameter
LINE_WIDTH = 2.0
HAIRLINE = 0.8


def palette(theme: Optional[str]) -> dict[str, Any]:
    return THEMES.get(str(theme or "light").lower(), THEMES["light"])


@dataclass
class ChartResult:
    """Outcome of one tool call: the image, the numbers, and why if it failed."""

    tool: str
    path: Optional[str]
    summary: dict[str, Any] = field(default_factory=dict)
    grounding: list[float] = field(default_factory=list)
    empty: bool = False
    reason: Optional[str] = None

    def as_tool_message(self) -> dict[str, Any]:
        """Compact payload fed back to the LLM for the narration round-trip."""
        payload = {"tool": self.tool, "summary": self.summary}
        if self.empty:
            payload["no_data"] = True
            payload["reason"] = self.reason
        else:
            payload["chart_saved_to"] = self.path
        return payload


def _money(value: float) -> str:
    """`-$967`, not `$-967` — the sign belongs outside the currency symbol."""
    return f"-${abs(value):,.0f}" if value < 0 else f"${value:,.0f}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def _style_axes(ax, pal: dict, money_axis: bool = True) -> None:
    """Recessive chrome: hairline solid grid, no box, muted tick text."""
    ax.set_facecolor(pal["surface"])
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color(pal["axis"])
    ax.spines["bottom"].set_linewidth(HAIRLINE)
    ax.grid(axis="y", color=pal["grid"], linewidth=HAIRLINE, linestyle="-")
    ax.set_axisbelow(True)
    ax.tick_params(colors=pal["muted"], labelsize=9, length=0)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_color(pal["muted"])
    if money_axis:
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: _money(v)))


def _title(ax, pal: dict, title: str, subtitle: str) -> None:
    """Title + subtitle stacked above the axes.

    Drawn as two positioned texts rather than `set_title` + an offset label:
    the latter let the subtitle ride up into the title's descenders.
    """
    ax.text(0, 1.155, title, transform=ax.transAxes, fontsize=13.5,
            fontweight="bold", color=pal["ink"], va="bottom", ha="left")
    ax.text(0, 1.045, subtitle, transform=ax.transAxes, fontsize=9.5,
            color=pal["muted"], va="bottom", ha="left")


def _legend(ax, pal: dict, order: Optional[list[str]] = None, **kwargs) -> None:
    """Legend with explicit series ordering.

    Matplotlib orders by artist creation, which puts an overlaid line before the
    bars it sits on top of; `order` restores the reading order.
    """
    # The donut supplies its own wedge handles/labels; everything else reads
    # them off the axes.
    handles = kwargs.pop("handles", None)
    labels = kwargs.pop("labels", None)
    if handles is None or labels is None:
        handles, labels = ax.get_legend_handles_labels()
    if order:
        pairs = dict(zip(labels, handles))
        handles = [pairs[name] for name in order if name in pairs]
        labels = [name for name in order if name in pairs]
    legend = ax.legend(handles, labels, frameon=False, fontsize=9, **kwargs)
    for text in legend.get_texts():           # text wears ink, never the series hue
        text.set_color(pal["ink_secondary"])


def _annotate(ax, pal: dict, x, y, text: str, dx: int = 0, dy: int = 11, ha: str = "center") -> None:
    ax.annotate(text, (x, y), textcoords="offset points", xytext=(dx, dy),
                ha=ha, va="center" if dy == 0 else "bottom",
                fontsize=9, fontweight="bold", color=pal["ink"])


class VisualizationTools:
    """Owns chart rendering for one `UserDataStore`."""

    def __init__(self, store: UserDataStore, output_dir: Path, dpi: int = 120):
        self.store = store
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.dpi = dpi

    # -- helpers --------------------------------------------------------------

    def _save(self, fig, user_id: str, name: str, pal: dict, theme: str) -> str:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")[:-3]
        # The random suffix means a chart URL cannot be derived from a user id
        # and a timestamp. It is defence in depth, not the access control --
        # that is the grant recorded in the cache and checked when serving.
        nonce = secrets.token_urlsafe(9).replace("-", "").replace("_", "")
        path = self.output_dir / f"{_slug(user_id)}_{name}_{_slug(theme)}_{stamp}_{nonce}.png"
        fig.savefig(path, dpi=self.dpi, bbox_inches="tight",
                    facecolor=pal["surface"], edgecolor="none")
        plt.close(fig)
        return str(path)

    def _figure(self, pal: dict, size: tuple[float, float]):
        fig, ax = plt.subplots(figsize=size)
        fig.patch.set_facecolor(pal["surface"])
        return fig, ax

    def _empty(
        self, tool: str, period: Period, extra: str = "", user_id: Optional[str] = None
    ) -> ChartResult:
        """An empty window, explained and redirected.

        "No transactions found" is true and useless on its own: it leaves
        someone guessing which window *would* work, and the usual next guess is
        another empty one. Naming the range the data actually covers turns a
        dead end into the next question, which is what the brief asks an empty
        result to do.
        """
        reason = f"No transactions found for {month_name(period)}."
        if extra:
            reason += f" {extra}"
        suggestion = self._coverage_hint(user_id)
        if suggestion:
            reason += f" {suggestion}"
        return ChartResult(
            tool=tool,
            path=None,
            summary={"period": period.label, "transaction_count": 0},
            empty=True,
            reason=reason,
        )

    def _coverage_hint(self, user_id: Optional[str]) -> str:
        """"Your data runs March–December 2025; try November 2025." """
        if not user_id:
            return ""
        try:
            first, last = self.store.date_range(user_id)
        except Exception:  # noqa: BLE001 -- a hint is never worth failing a turn for
            return ""
        if first is None or last is None:
            return ""
        span = (
            f"{first:%B %Y}"
            if (first.year, first.month) == (last.year, last.month)
            else f"{first:%B %Y} to {last:%B %Y}"
        )
        return f"This account has transactions from {span} — try {last:%B %Y}."

    # -- 1. monthly spending trend -------------------------------------------

    def plot_monthly_spending_trend(
        self,
        user_id: str,
        months: int = 1,
        category_filter: Optional[str] = None,
        rolling_window: int = 3,
        theme: str = "light",
    ) -> ChartResult:
        """"How has my spending changed over time?" — line + rolling average."""
        months = max(1, int(months))
        pal = palette(theme)
        period = resolve_period(None, self.store.as_of, months=months)
        frame = self.store.get_user_frame(
            user_id, period=period, parent_category=category_filter, include_income=False
        )
        if frame.empty:
            hint = f"No {category_filter} spending in this window." if category_filter else ""
            return self._empty("plot_monthly_spending_trend", period, hint, user_id=user_id)

        monthly = self.store.monthly_totals(frame)
        values = monthly["expense"].tolist()
        labels = monthly["month"].tolist()
        window = max(1, min(int(rolling_window), len(values)))
        rolling = monthly["expense"].rolling(window=window, min_periods=1).mean().tolist()
        x = list(range(len(values)))

        fig, ax = self._figure(pal, (9.2, 4.6))
        ax.fill_between(x, values, color=pal["net"], alpha=0.10, zorder=1)
        if len(values) > 1:
            ax.plot(x, rolling, linewidth=1.6, color=pal["muted"], zorder=2,
                    label=f"{window}-month rolling average")
        ax.plot(x, values, linewidth=LINE_WIDTH, color=pal["net"], zorder=3,
                solid_capstyle="round", solid_joinstyle="round",
                marker="o", markersize=MARKER_SIZE,
                markeredgecolor=pal["surface"], markeredgewidth=2,  # 2px surface ring
                label="Monthly spend")

        # Selective direct labels: the peak and the endpoint only.
        peak = int(monthly["expense"].idxmax())
        _annotate(ax, pal, peak, values[peak], _money(values[peak]))
        if peak != len(values) - 1:
            _annotate(ax, pal, len(values) - 1, values[-1], _money(values[-1]))

        scope = f" · {category_filter.title()}" if category_filter else ""
        _title(ax, pal, f"Monthly spending{scope}",
               f"{self.store.user_name(user_id)} · {month_name(period)}")
        ax.set_xticks(x, labels)
        ax.set_ylim(bottom=0)
        ax.margins(x=0.10)  # room for the endpoint label
        _style_axes(ax, pal)
        if len(values) > 1:
            _legend(ax, pal, order=["Monthly spend", f"{window}-month rolling average"],
                    loc="upper left", ncols=2, bbox_to_anchor=(0, -0.14))
        path = self._save(fig, user_id, "monthly_spending_trend", pal, theme)

        first, last = values[0], values[-1]
        change_pct = round((last - first) / first * 100, 1) if first else None
        summary = {
            "period": period.label,
            "period_label": month_name(period),
            "months_covered": len(values),
            "category_filter": category_filter,
            "monthly_totals": [{"month": m, "expense": round(v, 2)} for m, v in zip(labels, values)],
            "total_spend": round(sum(values), 2),
            "average_monthly_spend": round(sum(values) / len(values), 2),
            # `month` stays the machine key (`2025-07`); `label` is what prose
            # should quote, so answers don't read "peaking in 2025-07".
            "highest_month": {
                "month": labels[peak],
                "label": month_label(labels[peak]),
                "expense": round(values[peak], 2),
            },
            "change_pct_first_to_last": change_pct,
            "rolling_window": window,
        }
        grounding = values + rolling + [
            summary["total_spend"], summary["average_monthly_spend"], change_pct
        ]
        return ChartResult("plot_monthly_spending_trend", path, summary,
                           [g for g in grounding if g is not None])

    # -- 2. category breakdown ------------------------------------------------

    def plot_category_breakdown(
        self,
        user_id: str,
        period: str = "last_3_months",
        top_n: int = 7,
        parent_category: Optional[str] = None,
        theme: str = "light",
    ) -> ChartResult:
        """"Where is my money going?" — donut, total in the centre.

        `parent_category` drills into subcategories, which is how the brief's
        "show me my food spending -> top_subcategories with parent_category=Food"
        example is served without inventing a fourth tool.

        The legend carries the value and share for every segment: that is the
        required relief for the light-mode contrast warning on the lighter
        categorical slots, and it means nothing is encoded by colour alone.
        """
        pal = palette(theme)
        resolved = resolve_period(period, self.store.as_of)
        frame = self.store.get_user_frame(
            user_id, period=resolved, parent_category=parent_category, include_income=False
        )
        if frame.empty:
            hint = f"No {parent_category} spending in this window." if parent_category else ""
            return self._empty("plot_category_breakdown", resolved, hint, user_id=user_id)

        group_column = "subcategory" if parent_category else "parent_category"
        # Clamped here as well as in the schema: the dispatcher repairs
        # out-of-range arguments, but a model that asks for 2 should still get a
        # readable chart rather than one dominated by an "Other" wedge.
        top_n = max(4, min(int(top_n or 7), 20))
        rolled = self.store.taxonomy.rollup(
            frame, top_n=top_n, value_column="expense_amount", group_column=group_column
        )
        if rolled.empty:
            return self._empty("plot_category_breakdown", resolved, user_id=user_id)

        total = float(rolled["amount"].sum())
        labels = rolled["category"].tolist()
        amounts = rolled["amount"].tolist()
        shares = [round(a / total * 100, 1) for a in amounts]

        slots = pal["categorical"]
        colors = [
            pal["other"] if name == "Other" else slots[i % len(slots)]
            for i, name in enumerate(labels)
        ]

        fig, ax = self._figure(pal, (8.2, 5.0))
        wedges, _ = ax.pie(
            amounts,
            startangle=90,
            counterclock=False,
            colors=colors,
            # edgecolor == surface is the 2px *gap*, not a border drawn on the mark
            wedgeprops={"width": 0.38, "edgecolor": pal["surface"], "linewidth": 2},
        )
        ax.text(0, 0.10, _money(total), ha="center", va="center",
                fontsize=25, fontweight="bold", color=pal["ink"])
        ax.text(0, -0.16, "total spend", ha="center", va="center",
                fontsize=10, color=pal["muted"])

        _legend(
            ax, pal,
            handles=wedges,
            labels=[f"{n.title()}   {_money(a)}   ({s}%)" for n, a, s in zip(labels, amounts, shares)],
            loc="center left",
            bbox_to_anchor=(1.02, 0.5),
        )
        scope = f"{parent_category.title()} breakdown" if parent_category else "Where the money went"
        _title(ax, pal, scope, f"{self.store.user_name(user_id)} · {month_name(resolved)}")
        ax.set_aspect("equal")
        path = self._save(fig, user_id, "category_breakdown", pal, theme)

        summary = {
            "period": resolved.label,
            "period_label": month_name(resolved),
            "parent_category": parent_category,
            "grouped_by": group_column,
            "total_spend": round(total, 2),
            "categories": [
                {"name": n, "amount": round(a, 2), "share_pct": s}
                for n, a, s in zip(labels, amounts, shares)
            ],
            "top_category": {"name": labels[0], "amount": round(amounts[0], 2), "share_pct": shares[0]},
            "transaction_count": int(len(frame)),
        }
        return ChartResult("plot_category_breakdown", path, summary,
                           amounts + shares + [round(total, 2), len(frame)])

    # -- 3. income vs expense -------------------------------------------------

    def plot_income_vs_expense(
        self,
        user_id: str,
        months: int = 6,
        show_net_line: bool = True,
        theme: str = "light",
    ) -> ChartResult:
        """"Am I saving or bleeding money?" — grouped bars + net savings line.

        All three series are dollars, so they share **one** y-axis. The earlier
        twin-axis version made the net line's position relative to the bars
        arbitrary, which invents a relationship the data doesn't contain.
        """
        months = max(1, int(months))
        pal = palette(theme)
        period = resolve_period(None, self.store.as_of, months=months)
        frame = self.store.get_user_frame(user_id, period=period)
        if frame.empty:
            return self._empty("plot_income_vs_expense", period, user_id=user_id)

        monthly = self.store.monthly_totals(frame)
        labels = monthly["month"].tolist()
        income = monthly["income"].tolist()
        expense = monthly["expense"].tolist()
        net = monthly["net"].tolist()
        x = list(range(len(labels)))

        # Cap bar thickness so the band keeps its air; the gap between the pair
        # is surface, not a stroke.
        width = min(0.38, BAR_MAX_PX / (self.dpi * 0.55) / max(len(labels), 1) * 2.2)
        width = max(0.16, min(width, 0.38))
        gap = 0.02

        fig, ax = self._figure(pal, (9.6, 4.9))
        ax.bar([i - width / 2 - gap for i in x], income, width,
               label="Income", color=pal["income"], linewidth=0, zorder=3)
        ax.bar([i + width / 2 + gap for i in x], expense, width,
               label="Expense", color=pal["expense"], linewidth=0, zorder=3)

        if show_net_line:
            ax.plot(x, net, linewidth=LINE_WIDTH, color=pal["net"], zorder=4,
                    solid_capstyle="round", marker="o", markersize=MARKER_SIZE,
                    markeredgecolor=pal["surface"], markeredgewidth=2,
                    label="Net savings")
            # Sits in the right-hand margin rather than above the point, which
            # would land on top of the final month's expense bar.
            _annotate(ax, pal, x[-1], net[-1], _money(net[-1]), dx=10, dy=0, ha="left")

        ax.axhline(0, color=pal["axis"], linewidth=HAIRLINE, zorder=2)
        _title(ax, pal, "Income vs expense",
               f"{self.store.user_name(user_id)} · {month_name(period)}")
        ax.set_xticks(x, labels)
        # Explicit limits rather than margins: the net endpoint label needs
        # dedicated room to the right of the final bar pair, not a proportional
        # margin that shrinks as months are added.
        ax.set_xlim(-0.6, len(labels) - 1 + (1.05 if show_net_line else 0.6))
        _style_axes(ax, pal)
        _legend(ax, pal, order=["Income", "Expense", "Net savings"],
                loc="upper left", ncols=3, bbox_to_anchor=(0, -0.14))
        path = self._save(fig, user_id, "income_vs_expense", pal, theme)

        total_income = round(sum(income), 2)
        total_expense = round(sum(expense), 2)
        total_net = round(total_income - total_expense, 2)
        savings_rate = round(total_net / total_income * 100, 1) if total_income else None
        summary = {
            "period": period.label,
            "period_label": month_name(period),
            "months_covered": len(labels),
            "monthly": [
                {"month": m, "income": round(i, 2), "expense": round(e, 2), "net": round(n, 2)}
                for m, i, e, n in zip(labels, income, expense, net)
            ],
            "total_income": total_income,
            "total_expense": total_expense,
            "net_savings": total_net,
            "savings_rate_pct": savings_rate,
            "is_saving": total_net > 0,
            "months_in_deficit": int(sum(1 for n in net if n < 0)),
            "show_net_line": bool(show_net_line),
        }
        grounding = income + expense + net + [total_income, total_expense, total_net, savings_rate]
        return ChartResult("plot_income_vs_expense", path, summary,
                           [g for g in grounding if g is not None])

    # -- 4. top merchants -----------------------------------------------------

    def plot_top_merchants(
        self,
        user_id: str,
        period: str = "last_3_months",
        top_n: int = 8,
        parent_category: Optional[str] = None,
        theme: str = "light",
    ) -> ChartResult:
        """"Where am I actually spending it?" — ranked horizontal bars.

        Categories answer *what kind* of spending; merchants answer *who took
        the money*, which is the more actionable of the two and the one people
        ask second. Horizontal bars because merchant names are text of variable
        length: rotating them under a vertical axis makes them unreadable.
        """
        pal = palette(theme)
        resolved = resolve_period(period, self.store.as_of)
        frame = self.store.get_user_frame(
            user_id, period=resolved, parent_category=parent_category, include_income=False
        )
        if frame.empty:
            hint = f"No {parent_category} spending in this window." if parent_category else ""
            return self._empty("plot_top_merchants", resolved, hint, user_id=user_id)

        grouped = (
            frame.groupby("merchant_name", observed=True)
            .agg(amount=("expense_amount", "sum"), visits=("expense_amount", "size"))
            .sort_values("amount", ascending=False)
        )
        grouped = grouped[grouped["amount"] > 0]
        if grouped.empty:
            return self._empty("plot_top_merchants", resolved, user_id=user_id)

        total = float(frame["expense_amount"].sum())
        shown = grouped.head(max(1, int(top_n)))
        names = [str(n) for n in shown.index]
        amounts = [float(a) for a in shown["amount"]]
        visits = [int(v) for v in shown["visits"]]
        shares = [round(a / total * 100, 1) if total else 0.0 for a in amounts]

        # Tallest bar at the top: barh stacks from the bottom up, so the order
        # is reversed rather than the values re-sorted.
        height = max(2.6, 0.42 * len(names) + 1.5)
        fig, ax = self._figure(pal, (8.6, height))
        y = list(range(len(names)))
        ax.barh(y, amounts[::-1], height=0.62, color=pal["net"], linewidth=0, zorder=3)
        ax.set_yticks(y, [n[:26] for n in names[::-1]])

        for i, amount in enumerate(amounts[::-1]):
            ax.annotate(
                _money(amount), (amount, i), textcoords="offset points", xytext=(6, 0),
                va="center", ha="left", fontsize=9, fontweight="bold", color=pal["ink"],
            )

        scope = f"{parent_category.title()} merchants" if parent_category else "Where the money went"
        _title(ax, pal, scope, f"{self.store.user_name(user_id)} · {month_name(resolved)}")
        ax.set_facecolor(pal["surface"])
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.spines["bottom"].set_color(pal["axis"])
        ax.spines["bottom"].set_linewidth(HAIRLINE)
        ax.grid(axis="x", color=pal["grid"], linewidth=HAIRLINE, linestyle="-")
        ax.set_axisbelow(True)
        ax.tick_params(colors=pal["muted"], labelsize=9, length=0)
        for label in ax.get_xticklabels() + ax.get_yticklabels():
            label.set_color(pal["ink_secondary"])
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: _money(v)))
        ax.margins(x=0.14)  # room for the value labels
        path = self._save(fig, user_id, "top_merchants", pal, theme)

        summary = {
            "period": resolved.label,
            "period_label": month_name(resolved),
            "parent_category": parent_category,
            "total_spend": round(total, 2),
            "merchant_count": int(len(grouped)),
            "merchants": [
                {"name": n, "amount": round(a, 2), "share_pct": s, "visits": v}
                for n, a, s, v in zip(names, amounts, shares, visits)
            ],
            "top_merchant": {
                "name": names[0], "amount": round(amounts[0], 2),
                "share_pct": shares[0], "visits": visits[0],
            },
            "transaction_count": int(len(frame)),
        }
        return ChartResult("plot_top_merchants", path, summary,
                           amounts + shares + [round(total, 2), len(frame)])

    # -- 5. period comparison -------------------------------------------------

    def plot_period_comparison(
        self,
        user_id: str,
        period: str = "last_month",
        compare_to: Optional[str] = None,
        top_n: int = 8,
        theme: str = "light",
    ) -> ChartResult:
        """"Did I spend more this month than last?" — per-category change.

        Answers the comparison directly instead of drawing two charts and
        leaving the subtraction to the reader. `compare_to` defaults to the
        equal-length window immediately before `period`, because that is the
        only baseline that makes the delta meaningful.

        Drawn as diverging bars around a zero line: direction is the message,
        and a diverging form encodes it in position rather than only in colour,
        so it survives being read in greyscale or by a colour-blind reader.
        """
        pal = palette(theme)
        current = resolve_period(period, self.store.as_of)
        baseline = (
            resolve_period(compare_to, self.store.as_of)
            if compare_to
            else preceding_period(current)
        )

        now_frame = self.store.get_user_frame(user_id, period=current, include_income=False)
        was_frame = self.store.get_user_frame(user_id, period=baseline, include_income=False)
        if now_frame.empty and was_frame.empty:
            return self._empty("plot_period_comparison", current, user_id=user_id)

        def totals(frame):
            if frame.empty:
                return {}
            grouped = frame.groupby("parent_category", observed=True)["expense_amount"].sum()
            return {str(k): float(v) for k, v in grouped.items() if v > 0}

        now_by_cat, was_by_cat = totals(now_frame), totals(was_frame)
        now_total = float(now_frame["expense_amount"].sum()) if not now_frame.empty else 0.0
        was_total = float(was_frame["expense_amount"].sum()) if not was_frame.empty else 0.0

        # Union of both windows: a category that vanished is as interesting as
        # one that appeared, and dropping it would hide a real change.
        names = sorted(
            set(now_by_cat) | set(was_by_cat),
            key=lambda n: abs(now_by_cat.get(n, 0.0) - was_by_cat.get(n, 0.0)),
            reverse=True,
        )[: max(1, int(top_n))]

        rows = []
        for name in names:
            now_v, was_v = now_by_cat.get(name, 0.0), was_by_cat.get(name, 0.0)
            delta = now_v - was_v
            rows.append({
                "name": name,
                "current": round(now_v, 2),
                "previous": round(was_v, 2),
                "delta": round(delta, 2),
                # A category with no prior spend has an undefined percentage,
                # not an infinite one.
                "delta_pct": round(delta / was_v * 100, 1) if was_v else None,
            })
        rows.sort(key=lambda r: r["delta"], reverse=True)

        fig, ax = self._figure(pal, (9.0, max(2.8, 0.46 * len(rows) + 1.6)))
        y = list(range(len(rows)))
        ordered = rows[::-1]
        colors = [pal["expense"] if r["delta"] > 0 else pal["income"] for r in ordered]
        ax.barh(y, [r["delta"] for r in ordered], height=0.62, color=colors, linewidth=0, zorder=3)
        ax.set_yticks(y, [r["name"].title() for r in ordered])
        ax.axvline(0, color=pal["axis"], linewidth=HAIRLINE, zorder=4)

        for i, row in enumerate(ordered):
            offset = 6 if row["delta"] >= 0 else -6
            ax.annotate(
                ("+" if row["delta"] > 0 else "") + _money(row["delta"]),
                (row["delta"], i), textcoords="offset points", xytext=(offset, 0),
                va="center", ha="left" if row["delta"] >= 0 else "right",
                fontsize=9, fontweight="bold", color=pal["ink"],
            )

        _title(ax, pal, "What changed",
               f"{month_name(current)} vs {month_name(baseline)} · {self.store.user_name(user_id)}")
        ax.set_facecolor(pal["surface"])
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.spines["bottom"].set_color(pal["axis"])
        ax.spines["bottom"].set_linewidth(HAIRLINE)
        ax.grid(axis="x", color=pal["grid"], linewidth=HAIRLINE, linestyle="-")
        ax.set_axisbelow(True)
        ax.tick_params(colors=pal["muted"], labelsize=9, length=0)
        for label in ax.get_yticklabels() + ax.get_xticklabels():
            label.set_color(pal["ink_secondary"])
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: _money(v)))
        ax.margins(x=0.20)
        path = self._save(fig, user_id, "period_comparison", pal, theme)

        total_delta = now_total - was_total
        increases = [r for r in rows if r["delta"] > 0]
        decreases = [r for r in rows if r["delta"] < 0]
        summary = {
            "period": current.label,
            "period_label": month_name(current),
            "compare_period": baseline.label,
            "compare_period_label": month_name(baseline),
            "current_total": round(now_total, 2),
            "previous_total": round(was_total, 2),
            "delta": round(total_delta, 2),
            "delta_pct": round(total_delta / was_total * 100, 1) if was_total else None,
            "direction": "up" if total_delta > 0 else "down" if total_delta < 0 else "flat",
            "categories": rows,
            "biggest_increase": increases[0] if increases else None,
            "biggest_decrease": decreases[-1] if decreases else None,
            "transaction_count": int(len(now_frame)),
        }
        grounding = (
            [r["current"] for r in rows] + [r["previous"] for r in rows]
            + [abs(r["delta"]) for r in rows]
            + [round(now_total, 2), round(was_total, 2), abs(round(total_delta, 2))]
        )
        return ChartResult("plot_period_comparison", path, summary,
                           [g for g in grounding if g is not None])

    # -- 6. account-holder comparison (manager only) --------------------------

    def plot_user_comparison(
        self,
        user_id: str,
        other_user_id: str,
        period: str = "last_month",
        top_n: int = 8,
        theme: str = "light",
    ) -> ChartResult:
        """"How does Jose compare to Sarah?" -- two account holders, side by side.

        Only ever reachable by a caller holding `read:any`: the schema is not
        offered to anyone else, and the dispatcher refuses `other_user_id`
        without that scope. Both guards exist because either alone would be a
        single point of failure for reading somebody else's finances.
        """
        pal = palette(theme)
        resolved = resolve_period(period, self.store.as_of)

        left = self.store.get_user_frame(user_id, period=resolved, include_income=False)
        right = self.store.get_user_frame(other_user_id, period=resolved, include_income=False)
        if left.empty and right.empty:
            return self._empty("plot_user_comparison", resolved)

        def totals(frame):
            if frame.empty:
                return 0.0, {}
            grouped = frame.groupby("parent_category", observed=True)["expense_amount"].sum()
            return float(frame["expense_amount"].sum()), {
                str(k): float(v) for k, v in grouped.items() if v > 0
            }

        left_total, left_cats = totals(left)
        right_total, right_cats = totals(right)
        left_name = self.store.user_name(user_id)
        right_name = self.store.user_name(other_user_id)

        names = sorted(
            set(left_cats) | set(right_cats),
            key=lambda n: max(left_cats.get(n, 0.0), right_cats.get(n, 0.0)),
            reverse=True,
        )[: max(1, int(top_n))]

        rows = [
            {
                "name": n,
                "left": round(left_cats.get(n, 0.0), 2),
                "right": round(right_cats.get(n, 0.0), 2),
                "difference": round(left_cats.get(n, 0.0) - right_cats.get(n, 0.0), 2),
            }
            for n in names
        ]

        # Grouped horizontal bars: one row per category, two bars per row. The
        # shared axis is what makes "who spent more on X" answerable at a glance.
        height = max(3.0, 0.5 * len(rows) + 1.8)
        fig, ax = self._figure(pal, (9.0, height))
        y = list(range(len(rows)))
        bar_h = 0.36
        ordered = rows[::-1]
        ax.barh([i + bar_h / 2 for i in y], [r["left"] for r in ordered], bar_h,
                color=pal["net"], linewidth=0, zorder=3, label=left_name)
        ax.barh([i - bar_h / 2 for i in y], [r["right"] for r in ordered], bar_h,
                color=pal["expense"], linewidth=0, zorder=3, label=right_name)
        ax.set_yticks(y, [r["name"].title() for r in ordered])

        _title(ax, pal, "Side by side", f"{left_name} vs {right_name} · {month_name(resolved)}")
        ax.set_facecolor(pal["surface"])
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.spines["bottom"].set_color(pal["axis"])
        ax.spines["bottom"].set_linewidth(HAIRLINE)
        ax.grid(axis="x", color=pal["grid"], linewidth=HAIRLINE, linestyle="-")
        ax.set_axisbelow(True)
        ax.tick_params(colors=pal["muted"], labelsize=9, length=0)
        for label in ax.get_yticklabels() + ax.get_xticklabels():
            label.set_color(pal["ink_secondary"])
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: _money(v)))
        ax.margins(x=0.12)
        _legend(ax, pal, order=[left_name, right_name], loc="upper left",
                ncols=2, bbox_to_anchor=(0, -0.10))
        path = self._save(fig, user_id, "user_comparison", pal, theme)

        difference = left_total - right_total

        # "X spent N% more than Y" is a claim about the *lower* total: a gap of
        # 5,109 on totals of 8,563 and 13,672 is 59.7% more, not 37.4%. The
        # earlier field divided by the right-hand total, which is the answer to
        # a different question ("X spent 37.4% less"), and a model reading it
        # stated the wrong one. It was grounded -- the figure existed in the
        # data -- and still false, so the hallucination check could not catch
        # it. The fix is to name the quantity so it cannot be misread.
        higher_total, lower_total = max(left_total, right_total), min(left_total, right_total)
        gap = higher_total - lower_total
        if difference > 0:
            higher, lower = left_name, right_name
        elif difference < 0:
            higher, lower = right_name, left_name
        else:
            higher = lower = None

        summary = {
            "period": resolved.label,
            "period_label": month_name(resolved),
            "left_user_id": user_id,
            "left_user_name": left_name,
            "right_user_id": other_user_id,
            "right_user_name": right_name,
            "left_total": round(left_total, 2),
            "right_total": round(right_total, 2),
            "difference": round(difference, 2),
            "higher_spender": higher,
            "lower_spender": lower,
            "gap": round(gap, 2),
            # Explicitly: how much more the higher spender spent, as a
            # percentage of the lower spender's total. This is the number that
            # completes the sentence "X spent N% more than Y".
            "higher_spent_pct_more_than_lower": (
                round(gap / lower_total * 100, 1) if lower_total else None
            ),
            "categories": rows,
            "transaction_count": int(len(left) + len(right)),
        }
        grounding = (
            [r["left"] for r in rows] + [r["right"] for r in rows]
            + [abs(r["difference"]) for r in rows]
            + [round(left_total, 2), round(right_total, 2), abs(round(difference, 2))]
        )
        return ChartResult("plot_user_comparison", path, summary,
                           [g for g in grounding if g is not None])

    # -- 7. team overview (manager only) --------------------------------------

    def plot_team_overview(
        self,
        user_id: str,
        period: str = "last_3_months",
        highlight_user_id: Optional[str] = None,
        theme: str = "light",
    ) -> ChartResult:
        """"How does this account compare to the average?" -- everyone, ranked.

        Exists because the manager role may *ask* population questions and had
        no tool to answer one: "average of others" reached for the two-user
        comparison and produced an answer to a different question. A mean over
        the other account holders is a distinct quantity and needs its own tool.

        The average deliberately EXCLUDES the highlighted account. Comparing
        someone against an average they are inside of pulls the baseline toward
        them and understates the gap -- the more so the fewer accounts there are.
        """
        pal = palette(theme)
        resolved = resolve_period(period, self.store.as_of)
        focus = highlight_user_id or user_id

        totals: dict[str, float] = {}
        for uid in self.store.user_ids:
            frame = self.store.get_user_frame(uid, period=resolved, include_income=False)
            if not frame.empty:
                totals[uid] = float(frame["expense_amount"].sum())
        if not totals:
            return self._empty("plot_team_overview", resolved)

        others = {u: v for u, v in totals.items() if u != focus}
        peer_average = sum(others.values()) / len(others) if others else None
        focus_total = totals.get(focus)

        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        names = [self.store.user_name(u) for u, _ in ranked]
        values = [v for _, v in ranked]

        fig, ax = self._figure(pal, (9.0, max(2.6, 0.5 * len(ranked) + 1.6)))
        y = list(range(len(ranked)))
        colors = [
            pal["net"] if uid == focus else pal["other"] for uid, _ in ranked[::-1]
        ]
        ax.barh(y, values[::-1], height=0.6, color=colors, linewidth=0, zorder=3)
        ax.set_yticks(y, [n[:24] for n in names[::-1]])
        for i, value in enumerate(values[::-1]):
            ax.annotate(_money(value), (value, i), textcoords="offset points", xytext=(6, 0),
                        va="center", ha="left", fontsize=9, fontweight="bold", color=pal["ink"])
        if peer_average is not None:
            ax.axvline(peer_average, color=pal["expense"], linewidth=1.4, linestyle="--",
                       zorder=4, label=f"Average of the others ({_money(peer_average)})")
            _legend(ax, pal, loc="upper left", ncols=1, bbox_to_anchor=(0, -0.12))

        _title(ax, pal, "Across the team", f"{month_name(resolved)} · {len(totals)} account holders")
        ax.set_facecolor(pal["surface"])
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.spines["bottom"].set_color(pal["axis"])
        ax.spines["bottom"].set_linewidth(HAIRLINE)
        ax.grid(axis="x", color=pal["grid"], linewidth=HAIRLINE, linestyle="-")
        ax.set_axisbelow(True)
        ax.tick_params(colors=pal["muted"], labelsize=9, length=0)
        for label in ax.get_yticklabels() + ax.get_xticklabels():
            label.set_color(pal["ink_secondary"])
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: _money(v)))
        ax.margins(x=0.16)
        path = self._save(fig, user_id, "team_overview", pal, theme)

        above = (
            focus_total is not None and peer_average is not None and focus_total > peer_average
        )
        summary = {
            "period": resolved.label,
            "period_label": month_name(resolved),
            "account_holders": len(totals),
            "team_total": round(sum(totals.values()), 2),
            "team_average": round(sum(totals.values()) / len(totals), 2),
            # The baseline the comparison should actually use.
            "peer_average_excluding_focus": round(peer_average, 2) if peer_average is not None else None,
            "focus_user_id": focus,
            "focus_user_name": self.store.user_name(focus) if focus_total is not None else None,
            "focus_total": round(focus_total, 2) if focus_total is not None else None,
            "focus_vs_peer_average": (
                round(focus_total - peer_average, 2)
                if focus_total is not None and peer_average is not None
                else None
            ),
            "focus_is_above_average": above,
            "highest_spender": names[0],
            "lowest_spender": names[-1],
            "people": [
                {"user_id": uid, "name": self.store.user_name(uid), "total": round(v, 2)}
                for uid, v in ranked
            ],
        }
        return ChartResult("plot_team_overview", path, summary,
                           values + [v for v in (peer_average, focus_total) if v is not None])

    # -- dispatch table -------------------------------------------------------

    @property
    def registry(self) -> dict[str, Any]:
        return {
            "plot_monthly_spending_trend": self.plot_monthly_spending_trend,
            "plot_category_breakdown": self.plot_category_breakdown,
            "plot_income_vs_expense": self.plot_income_vs_expense,
            "plot_top_merchants": self.plot_top_merchants,
            "plot_period_comparison": self.plot_period_comparison,
            "plot_user_comparison": self.plot_user_comparison,
            "plot_team_overview": self.plot_team_overview,
        }
