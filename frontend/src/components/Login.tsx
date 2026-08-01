import { Suspense, lazy, useEffect, useState, type FormEvent } from "react";
import { api, ApiError, type LoginHints } from "../api/client";
import { writeToken } from "../auth/session";
import styles from "./Login.module.css";
import type { Route } from "../lib/routing";

const HeroField = lazy(() => import("./three/HeroField"));

export type Portal = "user" | "manager";

interface Props {
  portal: Portal;
  onSignedIn: () => void;
  onNavigate: (to: Route) => void;
  /** Set when the previous session expired mid-use rather than never existing. */
  expired?: boolean;
}

/* Two doors into the same building.
 *
 * They post to the same `/auth/login` — separating them is a matter of framing,
 * not of security, and it is worth being explicit about that. What the manager
 * portal adds is a **role check after authentication**: signing in there with an
 * ordinary account succeeds at the API and is then rejected here, because
 * dropping someone into a manager shell they have no scopes for produces a UI
 * full of buttons that all return 403. Better to say so at the door.
 */
const COPY: Record<Portal, {
  eyebrow: string;
  title: string;
  lede: string;
  cta: string;
  hint: string;
  otherLabel: string;
  otherRoute: Route;
}> = {
  user: {
    eyebrow: "Personal",
    title: "Sign in",
    lede: "Your transactions, and answers you can check.",
    cta: "Sign in",
    hint: "Ask about your own spending, income and savings.",
    otherLabel: "Manager or admin? Use the team sign-in",
    otherRoute: "/manager/login",
  },
  manager: {
    eyebrow: "Team",
    title: "Manager sign-in",
    lede: "Read any account, answer your team's questions, and import data.",
    cta: "Sign in to the team console",
    hint: "Requires a manager or admin account.",
    otherLabel: "Just looking at your own spending? Personal sign-in",
    otherRoute: "/login",
  },
};

const MANAGER_ROLES = new Set(["manager", "admin"]);

export default function Login({ portal, onSignedIn, onNavigate, expired }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [hints, setHints] = useState<LoginHints | null>(null);

  const copy = COPY[portal];

  // Absent by default: the endpoint 404s unless an operator turned hints on.
  useEffect(() => {
    api.hints().then(setHints).catch(() => setHints(null));
  }, []);

  // Each door offers the accounts that can actually get through it.
  const offered = (hints?.accounts ?? []).filter((a) =>
    portal === "manager" ? MANAGER_ROLES.has(a.role) : a.role === "user",
  );

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const session = await api.login(email.trim(), password);

      if (portal === "manager" && !MANAGER_ROLES.has(session.role)) {
        // Authenticated, but through the wrong door. Do not keep the token —
        // signing them in here would show a console every button of which
        // fails on scope.
        setError("That account isn't a manager. Use the personal sign-in instead.");
        setPassword("");
        return;
      }

      writeToken(session.access_token);
      onSignedIn();
    } catch (cause) {
      // The server returns one message for both a wrong password and an
      // unknown account, on purpose. Repeat it rather than guessing which.
      setError(
        cause instanceof ApiError && cause.status === 0
          ? "Could not reach the server."
          : (cause as Error).message,
      );
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`${styles.page} ${portal === "manager" ? styles.manager : ""}`}>
      <div className={styles.hero} aria-hidden="true">
        <Suspense fallback={null}>
          <HeroField />
        </Suspense>
      </div>

      <main className={styles.card}>
        <div className={styles.brand}>
          <span className={styles.mark} aria-hidden="true">
            ◗
          </span>
          <div>
            <strong>Ledger</strong>
            <span>{copy.eyebrow}</span>
          </div>
        </div>

        <h1>{copy.title}</h1>
        <p className={styles.lede}>{copy.lede}</p>

        {expired && !error && (
          <p className={styles.notice} role="status">
            Your session expired. Please sign in again.
          </p>
        )}

        <form onSubmit={submit} className={styles.form}>
          <label className={styles.field}>
            <span>Email</span>
            <input
              type="email"
              autoComplete="username"
              required
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder={portal === "manager" ? "manager@company.com" : "you@company.com"}
            />
          </label>

          <label className={styles.field}>
            <span>Password</span>
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
            />
          </label>

          {/* role=alert so a screen reader hears the failure without going
              looking for it. */}
          {error && (
            <p className={styles.error} role="alert">
              {error}
            </p>
          )}

          <button type="submit" className={styles.submit} disabled={busy}>
            {busy ? "Signing in…" : copy.cta}
          </button>
        </form>

        {offered.length > 0 && hints && (
          <div className={styles.hints}>
            <p className={styles.hintsLabel}>
              Sign in as — <span>click to fill</span>
            </p>
            <ul>
              {offered.map((account) => (
                <li key={account.email}>
                  <button
                    type="button"
                    onClick={() => {
                      setEmail(account.email);
                      setPassword(hints.password);
                      setError(null);
                    }}
                  >
                    <span className={styles.hintEmail}>{account.email}</span>
                    <span className={styles.hintRole}>{account.role}</span>
                  </button>
                </li>
              ))}
            </ul>
            <p className={styles.hintsWarn}>
              Credentials are shown because <code>SHOW_LOGIN_HINTS</code> is on. Turn it off
              before this is reachable by anyone else.
            </p>
          </div>
        )}

        <p className={styles.hint}>{copy.hint}</p>

        <button
          type="button"
          className={styles.switch}
          onClick={() => {
            setError(null);
            onNavigate(copy.otherRoute);
          }}
        >
          {copy.otherLabel}
        </button>
      </main>
    </div>
  );
}
