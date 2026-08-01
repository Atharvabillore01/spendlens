import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Everything the FastAPI app owns is proxied in dev, so `npm run dev` and the
// built bundle hit identical relative URLs — no environment-dependent base URL
// anywhere in the client.
// Anything the API owns. A route missing here does not fail loudly: the dev
// server falls back to the SPA and returns the HTML shell, so the client gets
// a page where it expected JSON. `/auth/*` going missing broke sign-in in dev
// while the built bundle — served by FastAPI itself — worked fine.
const API_ROUTES = [
  "/auth",
  "/query",
  "/requests",
  "/ingest",
  "/users",
  "/charts",
  "/healthz",
  "/readyz",
  "/metrics",
];

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    // The pipeline's own charts are the only images; keep everything inlined
    // below 4kb and emit stable, hashed asset names.
    assetsInlineLimit: 4096,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      API_ROUTES.map((route) => [
        route,
        { target: "http://127.0.0.1:8000", changeOrigin: true },
      ]),
    ),
  },
});
