/* Session state: the bearer token and who it says you are.
 *
 * The token lives in localStorage. That is a deliberate trade: it survives a
 * reload (a login that evaporates on refresh is unusable) at the cost of being
 * readable by any script on the page. The mitigation that matters is that the
 * token is short-lived and narrowly scoped — an httpOnly cookie would be
 * stronger, but it needs a same-origin cookie/CSRF design on the server, which
 * is the other agent's territory.
 *
 * `role` here drives *which screens are offered*, never what is permitted.
 * Every endpoint re-checks scope server-side, so tampering with this only
 * changes what buttons you see, not what they achieve.
 */

const TOKEN_KEY = "ledger.token";

export type Role = "user" | "manager" | "admin";

export interface Identity {
  tenant_id: string;
  user_id: string;
  email: string | null;
  role: Role | null;
  scopes: string[];
  can_read_all: boolean;
  authenticated: boolean;
}

export function readToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function writeToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode — the session lasts this tab only */
  }
}

/** Anonymous deployments (AUTH_REQUIRED=false) have an identity with no
 *  subject. Treated as "signed in with nothing special", not as logged out. */
export const ANONYMOUS: Identity = {
  tenant_id: "default",
  user_id: "",
  email: null,
  role: null,
  scopes: [],
  can_read_all: false,
  authenticated: false,
};

export function canUpload(identity: Identity | null): boolean {
  if (!identity) return false;
  return identity.scopes.includes("ingest:write") || identity.role === "admin";
}

export function canReadAll(identity: Identity | null): boolean {
  return Boolean(identity?.can_read_all);
}

export function isAdmin(identity: Identity | null): boolean {
  return identity?.role === "admin" || identity?.scopes.includes("admin") === true;
}
