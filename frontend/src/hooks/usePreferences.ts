import { useCallback, useEffect, useState } from "react";
import type { Theme } from "../types";

/* Theme and developer-panel state both live on <html> as data attributes: CSS
   reads them directly, and the inline script in index.html applies the stored
   value before first paint so a reload never flashes the wrong theme. These
   hooks own the write side and keep React in sync. */

const THEME_KEY = "ledger.theme";
const DEV_KEY = "ledger.dev";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null; // private mode
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode — the attribute still applies for this session */
  }
}

function systemTheme(): Theme {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** The theme actually in force, whether it came from the OS or the toggle. */
function resolveTheme(): Theme {
  const stamped = document.documentElement.dataset.theme;
  return stamped === "dark" || stamped === "light" ? stamped : systemTheme();
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(resolveTheme);

  // With no explicit override, follow the OS if it changes mid-session.
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      if (!read(THEME_KEY)) setTheme(systemTheme());
    };
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next: Theme = current === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      write(THEME_KEY, next);
      return next;
    });
  }, []);

  return { theme, toggle };
}

export function useDevMode() {
  const [dev, setDev] = useState<boolean>(() => document.documentElement.dataset.dev === "on");

  const toggle = useCallback(() => {
    setDev((current) => {
      const next = !current;
      document.documentElement.dataset.dev = next ? "on" : "off";
      write(DEV_KEY, next ? "on" : "off");
      return next;
    });
  }, []);

  return { dev, toggle };
}
