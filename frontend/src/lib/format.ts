/** `-$881.00`, not `$-881.00` — the sign belongs outside the symbol. */
export function money(value: number): string {
  const n = Number(value) || 0;
  return (
    (n < 0 ? "-$" : "$") +
    Math.abs(n).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  );
}

/** Display-size figures drop the cents: "$17,276" reads, "$17,276.00" doesn't. */
export function compactMoney(value: number): string {
  const n = Number(value) || 0;
  return (n < 0 ? "-$" : "$") + Math.round(Math.abs(n)).toLocaleString();
}

export function signedPct(value: number): string {
  return (value > 0 ? "+" : "") + value + "%";
}

const MONTHS_SHORT = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];
const MONTHS_LONG = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** `2025-11` -> `Nov` (axis) or `November 2025` (labels).
 *
 *  Month keys stay `YYYY-MM` on the wire — they are sorted, compared and
 *  cross-checked as machine values. This is only ever the display form. */
export function formatMonth(key: string, style: "short" | "long" = "short"): string {
  const match = /^(\d{4})-(\d{2})$/.exec(String(key));
  if (!match) return String(key);
  const year = match[1] as string;
  const index = Number(match[2]) - 1;
  if (index < 0 || index > 11) return String(key);
  return style === "long"
    ? `${MONTHS_LONG[index]} ${year}`
    : (MONTHS_SHORT[index] as string);
}

/** HOUSING -> Housing. Category names arrive upper-cased from the taxonomy. */
export function titleCase(text: string): string {
  const s = String(text);
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}

export function initials(name: string): string {
  return String(name)
    .split(/\s+/)
    .map((part) => part[0] ?? "")
    .slice(0, 2)
    .join("")
    .toUpperCase();
}

export function prefersReducedMotion(): boolean {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function uid(): string {
  return Math.random().toString(36).slice(2, 10);
}
