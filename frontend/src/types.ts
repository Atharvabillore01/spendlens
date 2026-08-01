/* Wire types. These mirror the FastAPI response models in `api.py` and the
   summary dicts built in `src/tools/visualizations.py` — if a field moves
   there, it moves here. */

export type ChartTool =
  | "plot_category_breakdown"
  | "plot_monthly_spending_trend"
  | "plot_income_vs_expense"
  | "plot_top_merchants"
  | "plot_period_comparison"
  | "plot_user_comparison"
  | "plot_team_overview";

export const CHART_TOOLS: ChartTool[] = [
  "plot_category_breakdown",
  "plot_monthly_spending_trend",
  "plot_income_vs_expense",
  "plot_top_merchants",
  "plot_period_comparison",
  "plot_user_comparison",
  "plot_team_overview",
];

/** Human titles. The wire uses tool names; the product never shows them. */
export const CHART_TITLES: Record<ChartTool, string> = {
  plot_category_breakdown: "Where the money went",
  plot_monthly_spending_trend: "Monthly spending",
  plot_income_vs_expense: "Income vs. expenses",
  plot_top_merchants: "Top merchants",
  plot_period_comparison: "What changed",
  plot_user_comparison: "Side by side",
  plot_team_overview: "Across the team",
};

export interface User {
  user_id: string;
  user_name: string;
  transaction_count: number;
}

export interface UsersResponse {
  users: User[];
  as_of: string;
}

export interface Health {
  ready: boolean;
  users: number;
  as_of: string;
  cache_backend: string;
  cache_ok: boolean;
  circuit_breaker: string;
  llm_configured: boolean;
  llm_live: boolean;
  models: string[];
}

/* ── chart summaries ─────────────────────────────────────────────── */

export interface CategorySlice {
  name: string;
  amount: number;
  share_pct: number;
}

export interface MonthExpense {
  month: string;
  expense: number;
}

export interface MonthFlow {
  month: string;
  income: number;
  expense: number;
  net: number;
}

interface SummaryBase {
  period: string;
  period_label?: string;
  transaction_count?: number;
  /** Set by the pipeline when the tool ran but the window held no rows. */
  no_data?: boolean;
}

export interface CategoryBreakdownSummary extends SummaryBase {
  parent_category: string | null;
  grouped_by: "parent_category" | "subcategory";
  total_spend: number;
  categories: CategorySlice[];
  top_category: CategorySlice;
}

export interface MonthlyTrendSummary extends SummaryBase {
  months_covered: number;
  category_filter: string | null;
  monthly_totals: MonthExpense[];
  total_spend: number;
  average_monthly_spend: number;
  highest_month: MonthExpense;
  change_pct_first_to_last: number | null;
  rolling_window: number;
}

export interface IncomeVsExpenseSummary extends SummaryBase {
  months_covered: number;
  monthly: MonthFlow[];
  total_income: number;
  total_expense: number;
  net_savings: number;
  savings_rate_pct: number | null;
  is_saving: boolean;
  months_in_deficit: number;
  show_net_line: boolean;
}

export interface MerchantSlice {
  name: string;
  amount: number;
  share_pct: number;
  visits: number;
}

export interface TopMerchantsSummary extends SummaryBase {
  parent_category: string | null;
  total_spend: number;
  merchant_count: number;
  merchants: MerchantSlice[];
  top_merchant: MerchantSlice;
}

export interface CategoryDelta {
  name: string;
  current: number;
  previous: number;
  delta: number;
  /** null when the baseline window had no spend — undefined, not infinite. */
  delta_pct: number | null;
}

export interface PeriodComparisonSummary extends SummaryBase {
  compare_period: string;
  compare_period_label?: string;
  current_total: number;
  previous_total: number;
  delta: number;
  delta_pct: number | null;
  direction: "up" | "down" | "flat";
  categories: CategoryDelta[];
  biggest_increase: CategoryDelta | null;
  biggest_decrease: CategoryDelta | null;
}

export interface UserDelta {
  name: string;
  left: number;
  right: number;
  difference: number;
}

export interface UserComparisonSummary extends SummaryBase {
  left_user_id: string;
  left_user_name: string;
  right_user_id: string;
  right_user_name: string;
  left_total: number;
  right_total: number;
  difference: number;
  higher_spender: string | null;
  lower_spender: string | null;
  gap: number;
  /** How much more the higher spender spent, as a share of the LOWER total —
   *  the number that completes "X spent N% more than Y". */
  higher_spent_pct_more_than_lower: number | null;
  categories: UserDelta[];
}

export interface TeamMember {
  user_id: string;
  name: string;
  total: number;
}

export interface TeamOverviewSummary extends SummaryBase {
  account_holders: number;
  team_total: number;
  team_average: number;
  /** The baseline a comparison should use: the mean of everyone *except* the
   *  account being compared, so they do not drag their own baseline. */
  peer_average_excluding_focus: number | null;
  focus_user_id: string;
  focus_user_name: string | null;
  focus_total: number | null;
  focus_vs_peer_average: number | null;
  focus_is_above_average: boolean;
  highest_spender: string;
  lowest_spender: string;
  people: TeamMember[];
}

/** Flat fallback the pipeline composes when no chart tool ran. */
export interface WindowSummary {
  period?: string;
  period_label?: string;
  transaction_count?: number;
  total_spend?: number;
  total_income?: number;
  net_savings?: number;
  top_categories?: { name: string; amount: number }[];
  top_category?: { name: string; amount: number; share_pct?: number };
}

export type DataSummary = WindowSummary & {
  plot_category_breakdown?: CategoryBreakdownSummary;
  plot_monthly_spending_trend?: MonthlyTrendSummary;
  plot_income_vs_expense?: IncomeVsExpenseSummary;
  plot_top_merchants?: TopMerchantsSummary;
  plot_period_comparison?: PeriodComparisonSummary;
  plot_user_comparison?: UserComparisonSummary;
  plot_team_overview?: TeamOverviewSummary;
};

/* ── query ───────────────────────────────────────────────────────── */

export interface QueryResult {
  user_id: string;
  user_name: string | null;
  response: string;
  data_summary: DataSummary;
  visualizations: string[];
  cache_hit: boolean;
  latency_ms: number;
  guardrail_flags: string[];
  model_used?: string | null;
  degraded?: boolean;
  error?: string | null;
  message?: string | null;
}

export interface QueryRequest {
  user_id: string;
  prompt: string;
  theme: "light" | "dark";
}

/* ── transcript ──────────────────────────────────────────────────── */

export type Turn =
  | { id: string; role: "user"; text: string }
  | { id: string; role: "pending"; prompt: string }
  | { id: string; role: "assistant"; result: QueryResult }
  | { id: string; role: "error"; text: string };

export type Theme = "light" | "dark";
