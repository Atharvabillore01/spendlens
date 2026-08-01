import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, setUnauthorizedHandler } from "./api/client";
import { ANONYMOUS, canReadAll, writeToken, type Identity } from "./auth/session";
import ChatHeader from "./components/ChatHeader";
import Composer from "./components/Composer";
import DevPanel from "./components/DevPanel";
import Inbox from "./components/Inbox";
import Login from "./components/Login";
import Sidebar from "./components/Sidebar";
import Transcript from "./components/Transcript";
import UploadPage from "./components/UploadPage";
import { useDevMode, useTheme } from "./hooks/usePreferences";
import { useTranscripts } from "./hooks/useTranscripts";
import { handleFor, mentionedUser } from "./lib/mentions";
import { uid } from "./lib/format";
import { useRoute } from "./lib/routing";
import styles from "./App.module.css";
import type { Health, User, UsersResponse } from "./types";

export type View = "chat" | "upload" | "inbox";

type Boot = "loading" | "signed-out" | "ready";

export default function App() {
  const { theme, toggle: toggleTheme } = useTheme();
  const { dev, toggle: toggleDev } = useDevMode();

  const [route, navigate] = useRoute();
  const [boot, setBoot] = useState<Boot>("loading");
  const [expired, setExpired] = useState(false);
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [view, setView] = useState<View>("chat");

  const [users, setUsers] = useState<User[]>([]);
  const [asOf, setAsOf] = useState<string>("");
  const [currentUser, setCurrentUser] = useState<string | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [cacheEpoch, setCacheEpoch] = useState(0);
  const [openQuestions, setOpenQuestions] = useState(0);
  const [toast, setToast] = useState<string | null>(null);
  // Seeded from the first poll rather than 0, so signing in with three waiting
  // questions doesn't announce them as if they just arrived.
  const seenCount = useRef<number | null>(null);

  const { turnsFor, append, resolvePending, dropPending, clear, clearAll } = useTranscripts();
  const inFlight = useRef<AbortController | null>(null);
  const composerRef = useRef<HTMLInputElement>(null);

  const readAll = canReadAll(identity);
  /* Threads are keyed by reader *and* subject. Keyed by subject alone, a
     manager's conversation about an account holder landed under that holder's
     key, and the holder saw it on their next sign-in. */
  const threadKey = useCallback(
    (subject: string) => `${identity?.user_id || "anon"}:${subject}`,
    [identity],
  );
  /* A manager reads every account, so their conversation is one console
     thread, not a thread per account holder. Ordinary users keep a thread of
     their own -- there is only ever one subject. */
  const conversationKey = readAll ? threadKey("console") : currentUser ? threadKey(currentUser) : null;
  const turns = conversationKey ? turnsFor(conversationKey) : [];
  const activeUser = useMemo(
    () => users.find((u) => u.user_id === currentUser) ?? null,
    [users, currentUser],
  );

  /* ── session ───────────────────────────────────────────────────── */

  // A 401 anywhere drops the whole app to the login screen once, rather than
  // every caller having to notice.
  useEffect(() => {
    setUnauthorizedHandler((hadToken) => {
      setIdentity(null);
      // Only an actually-rejected token counts as an expiry. A first visit with
      // no token is not a session that ended.
      setExpired(hadToken);
      setBoot("signed-out");
    });
  }, []);

  const loadIdentity = useCallback(async () => {
    try {
      const me = await api.me();
      setIdentity(me.authenticated ? me : ANONYMOUS);
      setBoot("ready");
      setExpired(false);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) {
        setBoot("signed-out");
        return;
      }
      // Auth is off entirely, or the endpoint is unreachable. Fall back to the
      // anonymous experience rather than blocking on a login that cannot work.
      setIdentity(ANONYMOUS);
      setBoot("ready");
    }
  }, []);

  useEffect(() => {
    void loadIdentity();
  }, [loadIdentity]);

  const signOut = useCallback(() => {
    // Send them back to the door they came through, so a manager signing out
    // does not land on the personal form and wonder where the console went.
    const back = canReadAll(identity) ? "/manager/login" : "/login";
    clearAll();
    writeToken(null);
    setIdentity(null);
    setExpired(false);
    setBoot("signed-out");
    setUsers([]);
    setCurrentUser(null);
    navigate(back);
  }, [identity, navigate, clearAll]);

  /* ── data ──────────────────────────────────────────────────────── */

  const loadUsers = useCallback(async () => {
    try {
      const data: UsersResponse = await api.users();
      setUsers(data.users);
      setAsOf(data.as_of);
      setCurrentUser((existing) => {
        if (existing && data.users.some((u) => u.user_id === existing)) return existing;
        // An ordinary signed-in user is pinned to themselves; only a manager
        // (or an anonymous demo) gets to pick.
        if (identity?.user_id && data.users.some((u) => u.user_id === identity.user_id)) {
          return identity.user_id;
        }
        return data.users[0]?.user_id ?? null;
      });
      setBootError(null);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) return;
      setBootError(
        cause instanceof ApiError && cause.status === 0
          ? "Could not reach the server. Is it running?  →  uvicorn api:app --reload"
          : `Could not load accounts (${(cause as Error).message}).`,
      );
    }
  }, [identity]);

  const refreshCounts = useCallback(async () => {
    // Polling with no session produces a 401 every 20 seconds — noise in the
    // console, and a pointless round trip. The identity load is what decides
    // whether we are signed in; until it succeeds there is nothing to count.
    if (!identity) return;
    try {
      const data = await api.listRequests();
      const next = readAll ? data.counts.open : data.counts.answered;
      const previous = seenCount.current;
      setOpenQuestions(next);

      if (previous !== null && next > previous) {
        const delta = next - previous;
        setToast(
          readAll
            ? `${delta} new question${delta === 1 ? "" : "s"} from your team`
            : `Your manager replied to ${delta === 1 ? "your question" : `${delta} questions`}`,
        );
      }
      seenCount.current = next;
    } catch {
      setOpenQuestions(0);
    }
  }, [readAll, identity]);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.health());
    } catch {
      setHealth(null);
    }
  }, []);

  useEffect(() => {
    if (boot !== "ready") return;
    void loadUsers();
    void refreshHealth();
    void refreshCounts();
  }, [boot, loadUsers, refreshHealth, refreshCounts]);

  // Poll for new questions/replies. Paused while the tab is hidden: a
  // backgrounded tab polling every 20s is pure waste, and the visibility
  // handler catches up the moment it is looked at again.
  useEffect(() => {
    if (boot !== "ready" || !identity) return;
    let timer = 0;
    const tick = () => {
      if (document.visibilityState === "visible") void refreshCounts();
      timer = window.setTimeout(tick, 20_000);
    };
    timer = window.setTimeout(tick, 20_000);
    const onVisible = () => {
      if (document.visibilityState === "visible") void refreshCounts();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [boot, identity, refreshCounts]);

  // Toasts clear themselves; the sidebar badge is the persistent record.
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 8000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  /* ── asking ────────────────────────────────────────────────────── */

  const ask = useCallback(
    async (prompt: string) => {
      const key = conversationKey;
      if (!key || busy) return;

      /* In the console, who the question is about comes from the text.
         `@sarah what did she spend` reads one account; the same question with
         no mention is a question about everyone, and is sent anchored to the
         roster so the cross-account tools have a frame of reference. An
         ordinary user has neither affordance: their subject is themselves and
         the server would refuse anything else. */
      let userId = currentUser;
      /* What actually goes to the model. A team question still has to be
         anchored to an account -- the cross-account tools compare *from*
         somewhere -- so without that framing the model reads an unqualified
         "who spent the most?" as a question about the anchor, and answers about
         one person on a screen that says team. The framing is not shown in the
         transcript: the question the manager asked is the one they see. */
      let outgoing = prompt;
      if (readAll) {
        const mentioned = mentionedUser(prompt, users);
        userId = mentioned?.user_id ?? users[0]?.user_id ?? currentUser;
        if (!mentioned) outgoing = `Across all accounts on the team: ${prompt}`;
      }
      if (!userId) return;

      const controller = new AbortController();
      inFlight.current = controller;
      setBusy(true);
      append(
        key,
        { id: uid(), role: "user", text: prompt },
        { id: uid(), role: "pending", prompt },
      );

      try {
        const result = await api.query({ user_id: userId, prompt: outgoing, theme }, controller.signal);
        resolvePending(key, { id: uid(), role: "assistant", result });
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          dropPending(key);
        } else {
          resolvePending(key, {
            id: uid(),
            role: "error",
            text: `That request didn't go through — ${(error as Error).message}`,
          });
        }
      } finally {
        inFlight.current = null;
        setBusy(false);
        setCacheEpoch((n) => n + 1);
        void refreshHealth();
      }
    },
    [append, busy, conversationKey, currentUser, dropPending, readAll, refreshHealth, resolvePending, theme, users],
  );

  const cancel = useCallback(() => inFlight.current?.abort(), []);

  /* ── keyboard ──────────────────────────────────────────────────── */

  const lastPrompt = useMemo(() => {
    for (let i = turns.length - 1; i >= 0; i -= 1) {
      const turn = turns[i];
      if (turn && turn.role === "user") return turn.text;
    }
    return "";
  }, [turns]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing =
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target?.isContentEditable;

      if (event.key === "/" && !typing && view === "chat") {
        event.preventDefault();
        composerRef.current?.focus();
        return;
      }
      if (event.key === "Escape" && inFlight.current) {
        event.preventDefault();
        cancel();
        return;
      }
      if (
        event.key === "ArrowUp" &&
        target === composerRef.current &&
        composerRef.current &&
        composerRef.current.value === "" &&
        lastPrompt
      ) {
        event.preventDefault();
        composerRef.current.value = lastPrompt;
        composerRef.current.setSelectionRange(lastPrompt.length, lastPrompt.length);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cancel, lastPrompt, view]);

  // Already signed in but parked on a login URL — a bookmarked /manager/login,
  // or the back button after signing in. Correct the address.
  //
  // In an effect, not in render: `navigate` sets state, and doing that during
  // the render of the same component is what produces React's "cannot update
  // while rendering" warning and an extra render pass.
  useEffect(() => {
    if (boot === "ready" && route !== "/") navigate("/", true);
  }, [boot, route, navigate]);

  /* ── render ────────────────────────────────────────────────────── */

  /* Naming an account in the console composes a question about them rather
     than switching the screen to them: the console's whole point is that one
     thread covers everybody, and the subject of the *next* question comes from
     the text. Ordinary users have no roster to click. */
  const [mentionSeed, setMentionSeed] = useState<{ text: string; nonce: number } | undefined>();

  const selectUser = useCallback(
    (userId: string) => {
      if (busy) return;
      if (readAll) {
        const named = users.find((u) => u.user_id === userId);
        if (named) {
          setMentionSeed({ text: `@${handleFor(named.user_name)}`, nonce: Date.now() });
          composerRef.current?.focus();
          return;
        }
      }
      setCurrentUser(userId);
      setCacheEpoch((n) => n + 1);
    },
    [busy, readAll, users],
  );

  if (boot === "loading") {
    return <div className={styles.booting} aria-busy="true" />;
  }

  if (boot === "signed-out") {
    return (
      <Login
        portal={route === "/manager/login" ? "manager" : "user"}
        expired={expired}
        onNavigate={(to) => {
          setExpired(false);
          navigate(to);
        }}
        onSignedIn={() => {
          navigate("/", true);
          void loadIdentity();
        }}
      />
    );
  }

  if (bootError) {
    return (
      <div className={styles.fatal}>
        <h2>Backend unreachable</h2>
        <p>{bootError}</p>
      </div>
    );
  }

  return (
    <div className={styles.shell}>
      {toast && (
        <div className={styles.toast} role="status" aria-live="polite">
          <span>{toast}</span>
          <button
            type="button"
            onClick={() => {
              setView("inbox");
              setToast(null);
            }}
          >
            Open
          </button>
          <button type="button" className={styles.toastClose} onClick={() => setToast(null)}>
            ×
          </button>
        </div>
      )}

      <Sidebar
        users={users}
        currentUser={currentUser}
        activeUser={activeUser}
        asOf={asOf}
        health={health}
        theme={theme}
        dev={dev}
        identity={identity}
        view={view}
        openQuestions={openQuestions}
        onSelectView={setView}
        onSelectUser={selectUser}
        onToggleTheme={toggleTheme}
        onToggleDev={toggleDev}
        onSignOut={signOut}
      />

      <main className={styles.chat} aria-label={view === "chat" ? "Conversation" : view}>
        {view === "chat" && (
          <>
            <ChatHeader
              user={activeUser}
              asOf={asOf}
              turnCount={turns.filter((t) => t.role === "user").length}
              onClear={() => conversationKey && clear(conversationKey)}
              console={readAll}
              accountCount={users.length}
            />
            <Transcript
              turns={turns}
              user={activeUser}
              busy={busy}
              onAsk={ask}
              console={readAll}
              users={users}
              onMention={(userName) => {
                setMentionSeed({ text: `@${handleFor(userName)}`, nonce: Date.now() });
                composerRef.current?.focus();
              }}
            />
            <Composer
              ref={composerRef}
              busy={busy}
              disabled={!currentUser && !readAll}
              hasThread={turns.length > 0}
              onAsk={ask}
              onCancel={cancel}
              mentionables={readAll ? users : undefined}
              seed={mentionSeed}
            />
          </>
        )}

        {view === "upload" && (
          <UploadPage
            onImported={() => {
              void loadUsers();
              setCacheEpoch((n) => n + 1);
            }}
          />
        )}

        {view === "inbox" && (
          <Inbox canReply={readAll} theme={theme} onCountsChanged={refreshCounts} />
        )}
      </main>

      {view === "chat" && <DevPanel userId={currentUser} epoch={cacheEpoch} />}
    </div>
  );
}
