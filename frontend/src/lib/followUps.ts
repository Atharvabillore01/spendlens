import { formatMonth, titleCase } from "./format";
import type { QueryResult } from "../types";

/** Follow-ups are derived from what the pipeline actually computed, so every
 *  suggestion is answerable from this user's data rather than a generic list. */
export function followUps(result: QueryResult): string[] {
  const summary = result.data_summary ?? {};
  const breakdown = summary.plot_category_breakdown;
  const trend = summary.plot_monthly_spending_trend;
  const income = summary.plot_income_vs_expense;
  const out: string[] = [];

  if (breakdown?.top_category && !breakdown.parent_category) {
    out.push(`Break down my ${titleCase(breakdown.top_category.name).toLowerCase()} spending`);
  }
  if (breakdown && !trend) out.push("How has that changed over time?");
  if (trend?.highest_month) out.push(`What drove ${formatMonth(trend.highest_month.month, "long")}?`);
  if (income && income.months_in_deficit > 0) out.push("Which months did I overspend?");
  if (income && !breakdown) out.push("Where is my money going?");
  if (!income) out.push("Am I saving money?");
  if (!summary.plot_top_merchants) out.push("Show me my top merchants");

  return out.slice(0, 3);
}
