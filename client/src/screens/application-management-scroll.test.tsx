// @vitest-environment jsdom

/**
 * After an update or a rollback the Updating panel loads
 * `/?open=application#updates`, and the screen scrolls its Updates section
 * into view so the outcome is seen without scrolling (#255). A visit from the
 * menu passes no section and must not scroll.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { ApplicationManagement } from "./ApplicationManagement";

const INSTANCE = {
  app_name: "Spend Tracker", version: "0.9.1",
  build: { commit: "abcdef1234", branch: "dev", committed_at: "2026-03-01T10:00:00", dirty: false, source: "git" },
  environment: "production", python: "3.12.1", platform: "Linux", schema_revision: "0001",
  started_at: "2026-03-01T10:00:00", process_id: 4242, database_url_scheme: "sqlite",
  engine: { name: "SQLite", version: "3.45.0", journal_mode: "wal", path: "/srv/ledger.sqlite3" },
  size: { total_bytes: 4096, main_bytes: 2048, wal_bytes: 2048, page_size: 1024, page_count: 4, free_pages: 1 },
  households: [
    { id: "h1", name: "Alice's house", transactions: 12, receipts: 2 },
    { id: "h2", name: "The flat", transactions: 3, receipts: 0 },
  ],
  places: [], packages: [], addresses: ["http://127.0.0.1:8860"],
  latest_backup: null, logging_style: "normal", logs: [],
  repository: "https://example.com/spend-tracker", author: "https://example.com/author",
};

const UPDATE = {
  case: "working", running: "0.9.1", protocol: 1, in_flight: false, status: null, report: null,
  outcome: null, backups: [],
  heartbeat: {
    fresh: true, updater_version: "0.9.1", engine: "docker-desktop", engine_version: "4.48.0",
    container: "spend-tracker-updater-1", socket: "ok", hook: false,
  },
};

function withQueries(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

let scrolled: HTMLElement[];

beforeEach(() => {
  scrolled = [];
  // jsdom has no layout, so no scrollIntoView: record what was asked to scroll.
  Element.prototype.scrollIntoView = vi.fn(function (this: HTMLElement) {
    scrolled.push(this);
  });
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path === "/admin/application") return INSTANCE;
    if (path === "/admin/application/logging")
      return { current: "normal", directory: "/srv/logs", styles: [], files: [], streams: [] };
    if (path === "/admin/application/update") return UPDATE;
    return [];
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
});

describe("Application management, opened after an update", () => {
  it("scrolls the Updates section into view once, when asked", async () => {
    const shown = vi.fn();
    render(withQueries(<ApplicationManagement section="updates" onSectionShown={shown} />));
    await screen.findByRole("heading", { name: "Updates" });

    expect(scrolled.map((node) => node.id)).toEqual(["updates"]);
    expect(scrolled[0].getAttribute("aria-labelledby")).toBe("updates-title");
    expect(shown).toHaveBeenCalledTimes(1);
  });

  it("does not scroll on a visit from the menu", async () => {
    render(withQueries(<ApplicationManagement />));
    await screen.findByRole("heading", { name: "Updates" });

    expect(scrolled).toEqual([]);
  });

  it("does not scroll again once the shell has cleared the section", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = (section: "updates" | null) => (
      <QueryClientProvider client={client}>
        <ApplicationManagement section={section} onSectionShown={() => {}} />
      </QueryClientProvider>
    );
    const { rerender } = render(view("updates"));
    await screen.findByRole("heading", { name: "Updates" });
    rerender(view(null));
    rerender(view(null));

    expect(scrolled.map((node) => node.id)).toEqual(["updates"]);
  });
});
