import { canReadAll, canUpload, type Identity } from "../auth/session";
import { initials } from "../lib/format";
import styles from "./Sidebar.module.css";
import type { Health, Theme, User } from "../types";
import type { View } from "../App";

interface Props {
  users: User[];
  currentUser: string | null;
  activeUser: User | null;
  asOf: string;
  health: Health | null;
  theme: Theme;
  dev: boolean;
  identity: Identity | null;
  view: View;
  openQuestions: number;
  onSelectView: (view: View) => void;
  onSelectUser: (userId: string) => void;
  onToggleTheme: () => void;
  onToggleDev: () => void;
  onSignOut: () => void;
}

export default function Sidebar({
  users,
  currentUser,
  activeUser,
  asOf,
  health,
  theme,
  dev,
  identity,
  view,
  openQuestions,
  onSelectView,
  onSelectUser,
  onToggleTheme,
  onToggleDev,
  onSignOut,
}: Props) {
  // An ordinary signed-in user is pinned to their own data, so the account
  // switcher is not merely hidden — there is nothing else they may select.
  const mayPickUser = canReadAll(identity) || !identity?.authenticated;
  const mayUpload = canUpload(identity);
  return (
    <aside className={styles.sidenav}>
      <div className={styles.brand}>
        <span className={styles.mark} aria-hidden="true">
          ◗
        </span>
        <div className={styles.brandText}>
          <strong>Ledger</strong>
          <span>spending assistant</span>
        </div>
      </div>

      <nav className={styles.section} aria-label="Sections">
        <ul className={styles.navList}>
          <NavLink label="Ask" icon="◗" active={view === "chat"} onClick={() => onSelectView("chat")} />
          <NavLink
            label={canReadAll(identity) ? "Questions" : "Ask your manager"}
            icon="✉"
            active={view === "inbox"}
            badge={openQuestions}
            onClick={() => onSelectView("inbox")}
          />
          {mayUpload && (
            <NavLink
              label="Add data"
              icon="⬆"
              active={view === "upload"}
              onClick={() => onSelectView("upload")}
            />
          )}
        </ul>
      </nav>

      {mayPickUser && (
      <nav className={styles.section} aria-label="Accounts">
        {/* In the console these are handles to address, not screens to switch
            to: clicking one starts a mention in the composer. */}
        <p className={styles.sectionLabel}>
          {canReadAll(identity) ? "Ask about" : "Accounts"}
        </p>
        <ul className={styles.navList}>
          {users.map((user) => (
            <li key={user.user_id}>
              <button
                type="button"
                className={styles.navItem}
                aria-current={user.user_id === currentUser}
                onClick={() => onSelectUser(user.user_id)}
              >
                <span className={styles.avatar}>{initials(user.user_name)}</span>
                <span className={styles.who}>
                  <b>{user.user_name}</b>
                  <small>{user.transaction_count.toLocaleString()} transactions</small>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </nav>
      )}

      {/* "This account" is a lie in the console, where the thread spans every
          account and the next question decides whose data it reads. */}
      {activeUser && !canReadAll(identity) && (
        <div className={styles.section}>
          <p className={styles.sectionLabel}>This account</p>
          <ul className={styles.factList}>
            <Fact label="Transactions" value={activeUser.transaction_count.toLocaleString()} />
            <Fact label="Data through" value={asOf || "—"} />
          </ul>
        </div>
      )}

      {/* Instrumentation — only when the developer panel is on. */}
      <div className={`${styles.section} devOnly`}>
        <p className={styles.sectionLabel}>Pipeline</p>
        <ul className={styles.factList}>
          <Fact label="Cache" value={health?.cache_backend ?? "—"} />
          <Fact label="Breaker" value={health?.circuit_breaker ?? "—"} />
          <Fact label="Model" value={health?.models?.[0]?.split("/").pop() ?? "—"} />
          <Fact label="Keys / user" value="3" />
        </ul>
      </div>

      {identity?.authenticated && (
        <div className={styles.account}>
          <span className={styles.accountEmail} title={identity.email ?? ""}>
            {identity.email}
          </span>
          <span className={styles.role}>{identity.role}</span>
          <button type="button" className={styles.signOut} onClick={onSignOut}>
            Sign out
          </button>
        </div>
      )}

      <div className={styles.foot}>
        <div className={`${styles.health} devOnly`} title={healthTitle(health)}>
          <span
            className={`${styles.dot} ${health ? (health.ready ? styles.ok : styles.bad) : styles.bad}`}
          />
          <span>{healthLabel(health)}</span>
        </div>
        <div className={styles.footActions}>
          <button
            type="button"
            className={styles.iconBtn}
            onClick={onToggleDev}
            aria-pressed={dev}
            title="Developer panel"
            aria-label="Toggle developer panel"
          >
            ⚙
          </button>
          <button
            type="button"
            className={styles.iconBtn}
            onClick={onToggleTheme}
            title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
            aria-label="Toggle theme"
          >
            {theme === "dark" ? "☀" : "☾"}
          </button>
        </div>
      </div>
    </aside>
  );
}

function NavLink({
  label,
  icon,
  active,
  badge,
  onClick,
}: {
  label: string;
  icon: string;
  active: boolean;
  badge?: number;
  onClick: () => void;
}) {
  return (
    <li>
      <button type="button" className={styles.navItem} aria-current={active} onClick={onClick}>
        <span className={styles.navIcon} aria-hidden="true">
          {icon}
        </span>
        <span className={styles.who}>
          <b>{label}</b>
        </span>
        {badge ? <span className={styles.badge}>{badge}</span> : null}
      </button>
    </li>
  );
}


function Fact({ label, value }: { label: string; value: string }) {
  return (
    <li>
      <span>{label}</span>
      <b>{value}</b>
    </li>
  );
}

function healthLabel(health: Health | null): string {
  if (!health) return "offline";
  if (!health.llm_live) return "offline · scripted";
  if (!health.llm_configured) return "no API key";
  return `live · ${health.models[0]?.split("/")[1] ?? health.models[0] ?? "model"}`;
}

function healthTitle(health: Health | null): string {
  if (!health) return "Pipeline unreachable";
  return `cache=${health.cache_backend} · breaker=${health.circuit_breaker} · models=${health.models.join(", ")}`;
}
