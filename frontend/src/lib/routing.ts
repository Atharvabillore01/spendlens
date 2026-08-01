import { useCallback, useEffect, useState } from "react";

/* A ~30-line path router.
 *
 * The app has three routes, no nested layouts and no route params. Pulling in
 * react-router for that would add a dependency and an abstraction for less than
 * a switch statement's worth of behaviour. If routing ever grows params or
 * nesting, swap this out — nothing outside this file knows how it works.
 *
 * `navigate` uses pushState so the back button behaves, and `popstate` keeps
 * React in sync when the user presses it.
 */

export type Route = "/" | "/login" | "/manager/login";

const ROUTES: Route[] = ["/", "/login", "/manager/login"];

export function normalize(pathname: string): Route {
  // Trailing slashes are the usual way a hand-typed URL misses.
  const trimmed = pathname.replace(/\/+$/, "") || "/";
  return (ROUTES as string[]).includes(trimmed) ? (trimmed as Route) : "/";
}

export function useRoute(): [Route, (to: Route, replace?: boolean) => void] {
  const [route, setRoute] = useState<Route>(() => normalize(window.location.pathname));

  useEffect(() => {
    const onPop = () => setRoute(normalize(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((to: Route, replace = false) => {
    if (normalize(window.location.pathname) === to) return;
    window.history[replace ? "replaceState" : "pushState"]({}, "", to);
    setRoute(to);
  }, []);

  return [route, navigate];
}
