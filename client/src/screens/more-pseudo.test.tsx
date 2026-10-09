// @vitest-environment jsdom

/**
 * #56's exit, screen by screen: no English left in en-XA on the remaining
 * screens. Each screen joins this file in the pull request that extracts it.
 * Names in the fixtures are data and pass through untouched.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

// The One-time Import's card is its own screen, extracted on its own.
vi.mock("./OneTimeImport", () => ({ OneTimeImport: () => null, NEW_ISSUE_URL: "https://example.com/new" }));

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { activate } from "../lib/i18n";
import type { Household } from "../lib/types";
import { untranslated } from "../test-pseudo";
import { Categories } from "./Categories";
import { PayeeCategorisation } from "./PayeeCategorisation";
import { Payees } from "./Payees";
import { Rules } from "./Rules";
import { HouseholdPage } from "./Household";
import { Admin } from "./Admin";
import { Reconcile } from "./Reconcile";
import { AccountImport } from "./AccountImport";
import { History } from "./History";
import { BackupList, SavePanel } from "./Backups";
import { ApplicationManagement } from "./ApplicationManagement";
import { Receipts } from "./Receipts";
import { ImportGuide, guideFor } from "./ImportGuide";
import { ImportGuideDocument } from "./importGuide/en";

const HOUSEHOLD = {
  id: "house-1",
  name: "Casa",
  base_currency: "EUR",
  date_format: "YYYY-MM-DD",
} as unknown as Household;

/** The fixtures' own words. */
const DATA = /^(Casa|Doe|Sam|Bakery|Cinema|Everyday|Groceries|Bills|Rent|Water|Amazon|Carrefour|Square|Santander|Bar|Marisol|SQ|WWW|AMAZON|COMPRA|INTERNET|PAGO|MOVIL|MARISOL|MADRID|CARREFOUR|CINEMA|BAKERY)$/;

function withQueries(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function left(root: HTMLElement = document.body): string[] {
  return untranslated(root).filter((word) => !DATA.test(word));
}

beforeEach(async () => {
  await activate("en-XA");
});

afterEach(async () => {
  cleanup();
  vi.clearAllMocks();
  await activate("en");
});

describe("in en-XA, the remaining screens show no English", () => {
  it("Categories, empty and full, and a category's panel", async () => {
    vi.mocked(api.get).mockResolvedValueOnce([]).mockResolvedValue({ categories: [] });
    const { unmount } = render(withQueries(<Categories household={HOUSEHOLD} />));
    await screen.findByRole("heading", { level: 1 });
    await new Promise((done) => setTimeout(done, 0));
    expect(left()).toEqual([]);
    unmount();

    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.includes("/stats/categories"))
        return {
          categories: [
            {
              category_id: "c1",
              transaction_count: 3,
              payee_count: 2,
              payees: [
                { key: "p1", name: "Bakery", transaction_count: 2 },
                { key: null, name: "Cinema", transaction_count: 1 },
              ],
              more_payees: 4,
            },
          ],
        };
      return [
        {
          id: "g1",
          name: "Everyday",
          sort_order: 0,
          categories: [
            { id: "c1", group_id: "g1", name: "Groceries", full_name: "Everyday: Groceries", sort_order: 0, archived: false, used_by: 3 },
            { id: "c2", group_id: "g1", name: "Rent", full_name: "Everyday: Rent", sort_order: 1, archived: false, used_by: 0 },
          ],
        },
        { id: "g2", name: "Bills", sort_order: 1, categories: [] },
      ];
    });
    render(withQueries(<Categories household={HOUSEHOLD} />));
    await screen.findByText("Groceries");
    expect(left()).toEqual([]);

    const row = screen.getByText("Groceries").closest("tr") as HTMLElement;
    fireEvent.click(row.querySelector("td.amount button.link")!);
    await screen.findByRole("dialog");
    expect(left()).toEqual([]);

    // The group's own panel, and a new category in it.
    fireEvent.click(document.querySelector("button.heading-link")!);
    expect(left()).toEqual([]);
    const addTo = document.querySelector(".card .row > button.link") as HTMLElement;
    fireEvent.click(addTo);
    expect(left()).toEqual([]);
  });

  it("Payees, with spellings to merge, and the merge dialogs", async () => {
    const bakery = { id: "p1", name: "Bakery", transfer_account_id: null, transaction_count: 3, rule_count: 1 };
    const cinema = { id: "p2", name: "Cinema", transfer_account_id: null, transaction_count: 1, rule_count: 0 };
    const leg = { id: "p3", name: "Sam", transfer_account_id: "a1", transaction_count: 2, rule_count: 0 };
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.endsWith("/payee-collisions")) return [{ key: "bakery", payees: [bakery, cinema] }];
      if (path.includes("/accounts")) return [{ id: "a1", name: "Doe", currency: "EUR" }];
      return [bakery, cinema, leg];
    });
    render(withQueries(<Payees household={HOUSEHOLD} />));
    await screen.findByText("Sam");
    expect(left()).toEqual([]);

    // Review the spellings, then merge one payee into another.
    fireEvent.click(document.querySelector("section.card td button")!);
    expect(left()).toEqual([]);
    cleanup();
    render(withQueries(<Payees household={HOUSEHOLD} />));
    await screen.findByText("Sam");
    const merge = Array.from(document.querySelectorAll(".card")).pop()!.querySelector("tbody td button")!;
    fireEvent.click(merge);
    expect(left()).toEqual([]);
  });

  it("Payee categorisation, with a breakdown", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.includes("/stats/payees"))
        return {
          payees: [
            {
              payee_id: "p1",
              transaction_count: 1234,
              category_count: 5,
              categories: ["Groceries", "Rent", "Water", "Bills", "Everyday"].map((name, at) => ({
                key: `c${at}`,
                name,
                transaction_count: 10 - at,
              })),
            },
          ],
        };
      return [{ id: "p1", name: "Bakery", transfer_account_id: null, transaction_count: 1234, rule_count: 0 }];
    });
    render(withQueries(<PayeeCategorisation household={HOUSEHOLD} />));
    await screen.findByText("Bakery");
    expect(left()).toEqual([]);
    fireEvent.click(document.querySelector("button.tally-more")!);
    expect(left()).toEqual([]);
  });

  it("Payee naming rules, a new rule of both kinds, and applying them", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.endsWith("/payee-rules"))
        return [
          { id: "r1", match_type: "contains", action: "map", pattern: "BAKERY", payee_id: "p1", replacement: null, priority: 1, enabled: true },
          { id: "r2", match_type: "prefix", action: "rewrite", pattern: "SQ", payee_id: null, replacement: null, priority: 2, enabled: false },
        ];
      if (path.endsWith("/payee-suggestions"))
        return [{ pattern: "CINEMA", match_type: "contains", strings: 3, transactions: 9, payees: 3, examples: ["CINEMA 1", "CINEMA 2"] }];
      return [{ id: "p1", name: "Bakery", transfer_account_id: null, transaction_count: 3, rule_count: 1 }];
    });
    vi.mocked(api.post).mockResolvedValue({ considered: 40, changing: 3, moves: [{ to_name: "Bakery" }], orphaned: ["x"] });
    render(withQueries(<Rules household={HOUSEHOLD} />));
    await screen.findByText("BAKERY");
    expect(left()).toEqual([]);

    // The new-rule panel, naming a payee and then taking a rail off.
    fireEvent.click(document.querySelector("h1 + button")!);
    expect(left()).toEqual([]);
    const action = document.querySelectorAll(".panel select")[0] as HTMLSelectElement;
    fireEvent.change(action, { target: { value: "rewrite" } });
    expect(left()).toEqual([]);
    cleanup();

    // Applying the rules to what is already here.
    render(withQueries(<Rules household={HOUSEHOLD} />));
    await screen.findByText("CINEMA");
    fireEvent.click(document.querySelector(".card .row .small-button")!);
    await screen.findByText("x", { exact: false }).catch(() => undefined);
    await new Promise((done) => setTimeout(done, 0));
    expect(left()).toEqual([]);
  });

  it("the household page: what is in it, its settings and who is in it", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.endsWith("/stats"))
        return {
          members: 2, accounts: 3, accounts_closed: 1, transactions: 1234, transactions_uncleared: 5,
          first_transaction: "2026-01-02", last_transaction: "2026-03-02", receipts: 4, receipts_unattached: 1,
          payees: 9, payee_rules: 3, payee_rules_enabled: 2, categories: 12, categories_archived: 1,
          category_groups: 3, reconciliations: 2,
          currencies: [{ currency: "EUR", accounts: 2, transactions: 1000 }, { currency: "SEK", accounts: 1, transactions: 234 }],
          countries: [{ code: "ES", name: "Spain", flag: "" }],
        };
      if (path === "/themes") return [];
      if (path.endsWith("/members"))
        return [
          { user_id: "u1", display_name: "Sam", email: "sam@example.com", role: "owner", transactions_logged: 10, transactions_by_agent: 2 },
          { user_id: "u2", display_name: "Doe", email: "doe@example.com", role: "member", transactions_logged: 0, transactions_by_agent: 0 },
        ];
      return [];
    });
    const household = { ...HOUSEHOLD, receipts_keep_original: false, receipts_keep_original_forced: false, receipts_with_original: 3, theme: "default", accent: null, note: null } as unknown as Household;
    render(
      withQueries(
        <HouseholdPage household={household} user={{ id: "u1", display_name: "Sam" } as never} onChanged={vi.fn()} />,
      ),
    );
    await screen.findByText("doe@example.com");
    await screen.findByText("SEK");
    expect(left()).toEqual([]);
  });

  it("Admin: people, households and invitations, with their dialogs", async () => {
    const sam = { id: "u1", email: "sam@example.com", display_name: "Sam", role: "owner", created_at: "2026-01-02T10:00:00", disabled_at: null, recovery_codes_left: 7, households: ["h1"] };
    const doe = { ...sam, id: "u2", email: "doe@example.com", display_name: "Doe", role: "member", disabled_at: "2026-02-02T10:00:00", recovery_codes_left: 0, households: [] };
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/admin/users") return [sam, doe];
      if (path === "/admin/households") return [{ id: "h1", name: "Casa", base_currency: "EUR", date_format: "YYYY-MM-DD", created_at: "2026-01-02T10:00:00", member_ids: ["u1"] }];
      if (path === "/admin/resets")
        return [{ id: "r1", user_id: "u2", display_name: "Doe", email: "doe@example.com", password: true, authenticator: false, issued_by: null, created_at: "2026-03-01T10:00:00", expires_at: "2026-03-02T10:00:00", expired: true }];
      if (path === "/admin/sign-in-changes")
        return [{ id: 1, key: "k1", what: "promoted", password: false, authenticator: false, user_id: "u2", user_name: "Doe", by_id: "u1", by_name: "Sam", from_server: false, at: "2026-03-01T10:00:00" }];
      if (path === "/admin/invitations")
        return [{ id: "i1", email: null, role: "member", invited_by_id: "u1", household_ids: null, created_at: "2026-03-01T10:00:00", expires_at: "2026-03-08T10:00:00", accepted_at: null }];
      return [];
    });
    const dates = (word: string) => !/^(AM|PM|at)$/.test(word);
    render(withQueries(<Admin user={sam as never} />));
    await screen.findByText("doe@example.com");
    expect(left().filter(dates)).toEqual([]);

    // Each tab in turn.
    const tabs = Array.from(document.querySelectorAll<HTMLButtonElement>(".row button")).slice(0, 3);
    for (const tab of tabs.slice(1)) {
      fireEvent.click(tab);
      await new Promise((done) => setTimeout(done, 0));
      expect(left().filter(dates)).toEqual([]);
    }
  });

  it("reconciling an account, before and after the balance is typed", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.includes("/reconciliations")) return [{ id: "z1", account_id: "a", statement_date: "2026-01-31", statement_balance: 5000, batch_id: null }];
      return {
        locked_balance: 5000,
        last_statement_date: "2026-01-31",
        last_statement_balance: 5000,
        candidates: [{ id: "t1", date: "2026-02-03", payee: "Bakery", memo: "Sam", amount: -1200, cleared: "cleared" }],
      };
    });
    const account = { id: "a", name: "Casa", currency: "EUR" } as never;
    render(withQueries(<Reconcile account={account} onClose={vi.fn()} onDone={vi.fn()} />));
    await screen.findByText("Bakery");
    expect(left()).toEqual([]);
    const balance = document.querySelectorAll(".panel input")[1] as HTMLInputElement;
    fireEvent.change(balance, { target: { value: "38.00" } });
    expect(left()).toEqual([]);
  });

  it("importing accounts from a file, with a problem in it", async () => {
    vi.mocked(api.get).mockResolvedValue([{ code: "ES", name: "Spain", flag: "" }]);
    vi.mocked(api.upload).mockResolvedValue({
      rows: [
        { line: 2, name: "Casa", type: "checking", currency: "EUR", country: "ES", flag: "", opening_balance: 1000, opening_date: "2026-01-02", iban: null, problems: [] },
        { line: 3, name: "Doe", type: "boat", currency: "EUR", country: null, flag: "", opening_balance: null, opening_date: null, iban: null, problems: ["Sam"] },
      ],
    } as never);
    render(withQueries(<AccountImport household={HOUSEHOLD} onClose={vi.fn()} onImported={vi.fn()} />));
    const file = new File(["x"], "accounts.csv", { type: "text/csv" });
    fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [file] } });
    await screen.findByText("Doe");
    // The template's own column names stay as the file writes them.
    const left2 = left().filter((word) => !/^(name|type|currency|country|opening_balance|opening_date|checking|savings|cash|credit_card|other_asset|other_liability|boat|accounts|csv|Spain)$/.test(word));
    expect(left2).toEqual([]);
  });

  it("History's list and a batch's panel: the kind, the fields, the table and the sentences in en-XA", async () => {
    // The server's English, with the structure beside it (#57, #266). The
    // English strings here are deliberately not the real ones: en-XA must
    // word every sentence from its structure, never show the server's text.
    const money = (amount: number) => ({ type: "money", amount, currency: "EUR" });
    const batch = {
      id: "b1", kind: "import", status: "applied", actor_id: "u1", started_at: "2026-03-01T10:00:00",
      finished_at: null, source: null, summary: null, undone_by_id: null,
      headline: "Statement import", headline_key: "import", detail: "English detail", actor_name: "Sam", via: "Casa", change_count: 3,
      detail_phrase: {
        key: "history.detail.applied_by",
        params: {
          sentence: {
            key: "history.detail.import",
            params: {
              made: {
                type: "list",
                sep: ", ",
                items: [
                  { key: "history.import.created", params: { count: 2 } },
                  { key: "history.import.matched", params: { count: 1 } },
                ],
              },
              account: { type: "name", value: "Santander" },
              filename: { type: "name", value: "Doe" },
            },
          },
          who: { type: "name", value: "Sam" },
        },
      },
    };
    const line = {
      key: "history.line.fields",
      params: {
        label: { type: "list", sep: " · ", items: [money(-1250), { type: "name", value: "Bakery" }, { key: "history.label.in_account", params: { account: { type: "name", value: "Casa" } } }] },
        fields: {
          type: "list",
          sep: ", ",
          items: [
            {
              key: "history.field_change",
              params: {
                field: { type: "word", set: "field", key: "category_id" },
                was: { type: "word", set: "empty", key: "category_id" },
                now: { type: "category", group: "Everyday", name: "Cinema" },
              },
            },
            {
              key: "history.field_change",
              params: {
                field: { type: "word", set: "field", key: "cleared" },
                was: { type: "enum", column: "cleared", value: "uncleared" },
                now: { type: "enum", column: "cleared", value: "cleared" },
              },
            },
          ],
        },
      },
    };
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.includes("/batches/b1"))
        return {
          ...batch,
          change_count: 3,
          changed_rows: [
            {
              seq: 1, op: "update", table: "transaction", table_key: "transactions", row_id: "t1", summary: "English summary",
              summary_phrase: line,
              fields: [
                {
                  field: "category", column: "category_id", was: "English was", now: "English now",
                  was_value: { type: "word", set: "empty", key: "category_id" },
                  now_value: { type: "category", group: "Everyday", name: "Cinema" },
                },
                {
                  field: "reimbursed by", column: "reimbursed_by_id", was: "English was", now: "English now",
                  was_value: { type: "word", set: "removed", key: "transaction" },
                  now_value: { key: "history.payment", params: { amount: money(32450), account: { type: "name", value: "Santander" }, date: { type: "date", value: "2026-09-30", style: "medium" } } },
                },
              ],
              snapshot: [], redacted: ["password_hash"],
            },
            {
              seq: 2, op: "insert", table: "payee", table_key: "payees", row_id: "p1", summary: "English summary",
              summary_phrase: { key: "history.line.added", params: { table: { type: "word", set: "table", key: "payees" }, label: { type: "name", value: "Doe" } } },
              fields: [], snapshot: [{ field: "memo", column: "memo", was: "", now: "Doe", was_value: null, now_value: { type: "text", value: "Doe" } }], redacted: [],
            },
          ],
        };
      return [batch];
    });
    render(withQueries(<History household={HOUSEHOLD} />));
    await screen.findByText(/Santander/);
    const data = (word: string) => !/^(AM|PM|at|update|insert|password|hash|Sep)$/.test(word);
    expect(left().filter(data)).toEqual([]);
    expect(document.body.textContent).not.toContain("Statement import");
    expect(document.body.textContent).not.toContain("English");
    fireEvent.click(document.querySelector("tbody td.editable button, tbody button")!);
    await screen.findByRole("dialog");
    await screen.findAllByText(/Cinema/);
    expect(left().filter(data)).toEqual([]);
    expect(document.body.textContent).not.toContain("English");
    // The money is formatted here, from minor units, never sent as text.
    expect(document.body.textContent).toMatch(/12[.,]50/);
    expect(document.body.textContent).toMatch(/324[.,]50/);
  });

  it("History's sentences fall back to the server's English when a key is unknown to this build", async () => {
    const { detailOf } = await import("../lib/historyWords");
    expect(detailOf({ detail: "As the server said", detail_phrase: { key: "history.not.yet", params: {} } })).toBe(
      "As the server said",
    );
    await activate("en");
    expect(
      detailOf({ detail: "As the server said", detail_phrase: { key: "history.detail.nothing", params: {} } }),
    ).toBe("As the server said");
  });

  it("the backups list, with and without the key, and the save panel", () => {
    const backup = { name: "casa-2026-03-01.sqlite3", path: "/x", bytes: 1536, made_at: "2026-03-01T10:00:00" };
    render(withQueries(<BackupList backups={[backup]} onChanged={vi.fn()} />));
    const dates = (word: string) => !/^(AM|PM|at|casa|sqlite|KiB|zip|key|secret|README)$/.test(word);
    expect(left().filter(dates)).toEqual([]);
    cleanup();
    render(withQueries(<SavePanel backup={backup} withKey routes={["share", "folder", "download"]} onClose={vi.fn()} />));
    expect(left().filter(dates).filter((word) => !/^(Google|Drive|Dropbox|desktop)$/.test(word))).toEqual([]);
  });

  it("application management, every section, with the logs and a package list open", async () => {
    const instance = {
      app_name: "Casa", version: "9.9.9",
      build: { commit: "abcdef1234", branch: "dev", committed_at: "2026-03-01T10:00:00", dirty: true, source: "git" },
      environment: "dev", python: "3.12.1", platform: "Linux", schema_revision: "0001", started_at: "2026-03-01T10:00:00",
      process_id: 4242, database_url_scheme: "sqlite",
      engine: { name: "SQLite", version: "3.45.0", journal_mode: "wal", path: "/srv/casa.sqlite3" },
      size: { total_bytes: 4096, main_bytes: 2048, wal_bytes: 2048, page_size: 1024, page_count: 4, free_pages: 1 },
      households: [{ id: "h1", name: "Casa", transactions: 1234, receipts: 2 }],
      places: [{ what: "Doe", path: "/srv/doe", exists: false, bytes: null, note: "Sam", optional: false }],
      packages: [{ name: "fastapi", version: "1.0" }],
      addresses: ["http://127.0.0.1:8860"],
      latest_backup: null, logging_style: "normal", logs: [],
      repository: "https://example.com/casa", author: "https://example.com/sam",
    };
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/admin/application") return instance;
      if (path === "/admin/application/logging")
        return {
          current: "normal", directory: "/srv/logs",
          styles: [{ key: "normal", label: "Sam", blurb: "Doe", echo_sql: true }],
          files: [{ name: "app.log", bytes: 1536, modified: "2026-03-01T10:00:00" }],
          streams: [{ key: "sql", label: "Sam", filename: "sql.log", blurb: "Doe", holds_ledger_values: true }],
        };
      if (path.startsWith("/admin/application/logs/")) return { name: "app.log", bytes: 3, text: "Sam" };
      // The Updates section (#166): a working updater, so its sentence shows.
      if (path === "/admin/application/update")
        return {
          case: "working", running: "9.9.9", protocol: 1, in_flight: false, status: null, report: null,
          outcome: null, backups: [],
          heartbeat: {
            fresh: true, updater_version: "9.9.9", engine: "docker-desktop", engine_version: "4.48.0",
            container: "casa-updater-1", socket: "ok", hook: false,
          },
        };
      return [];
    });
    const release = (version: string) => ({
      version, tag: `v${version}`, name: null, published_at: "2026-03-01T10:00:00", notes: "Sam", notes_from: "changelog",
    });
    vi.mocked(api.post).mockResolvedValue({
      checked_at: "2026-03-01T10:00:00", running: "9.9.9", latest: "9.9.11", newer: true, problem: null,
      releases: [release("9.9.11"), release("9.9.10")],
      updater: { version: null, compatible: null, note: "Sam" },
    });
    render(withQueries(<ApplicationManagement />));
    await screen.findByText("app.log");
    // Paths, file names, versions and the engine's own words are data.
    const data = (word: string) =>
      !/^(AM|PM|at|srv|casa|sqlite|doe|logs|app|log|sql|SQLite|wal|dev|git|Linux|fastapi|http|https|example|com|sam|abcdef|txt|requirements|make|restore|secret|key|kill|lsof|Python|AGPL|GitHub|KiB|B|Docker|Desktop|updater)$/.test(word);
    expect(left().filter(data)).toEqual([]);

    fireEvent.click(document.querySelector("p.muted.small > button.link")!);
    const read = Array.from(document.querySelectorAll<HTMLButtonElement>("td.row-actions button"))[0];
    fireEvent.click(read);
    await screen.findByText("Sam", { selector: "pre" });
    const sections = document.querySelectorAll("section.card");
    fireEvent.click(sections[3].querySelector("button")!);
    await screen.findByRole("option", { name: "9.9.10" });
    screen.getByText("fastapi");
    expect(left().filter(data)).toEqual([]);
  });

  it("Receipts: both views, a selection and its dialog, and a receipt's panel", async () => {
    const one = {
      id: "r1", household_id: "house-1", transaction_id: null, content_sha256: "0123456789abcdef",
      original_filename: "casa.pdf", media_type: "application/pdf", byte_size: 2048, width: 600, height: 800,
      page_count: 3, captured_at: "2026-03-01T10:00:00", captured_at_is_local: true,
      gps_lat: 40.4, gps_lon: -3.7, gps_accuracy_m: 2500, gps_bearing: 90, camera: "Doe",
      exif: { SpendTrackerLocationSource: "device" }, client_encoded: true, note: null,
      uploaded_by_id: "u1", uploaded_by_name: "Sam", created_at: "2026-03-02T10:00:00",
      download_name: "casa/2026-03-01.pdf", has_original: true, download_bytes: 1536, also_on: 0,
    };
    const two = { ...one, id: "r2", transaction_id: "t1", captured_at: null, gps_lat: null, gps_lon: null, camera: null, page_count: 1 };
    vi.mocked(api.get).mockResolvedValue([one, two]);
    render(withQueries(<Receipts household={HOUSEHOLD} />));
    await screen.findAllByRole("checkbox");
    const data = (word: string) =>
      !/^(AM|PM|at|PDF|SHA|casa|pdf|application|KB|km|Google|Maps)$/.test(word);
    expect(left().filter(data)).toEqual([]);

    // Pick both, then ask to delete them.
    for (const box of Array.from(document.querySelectorAll<HTMLInputElement>(".card-pick input")))
      fireEvent.click(box);
    expect(left().filter(data)).toEqual([]);
    fireEvent.click(document.querySelector(".banner button.danger")!);
    await screen.findByRole("dialog");
    expect(left().filter(data)).toEqual([]);
    cleanup();

    // The list view, and the first receipt's panel with everything the camera wrote.
    render(withQueries(<Receipts household={HOUSEHOLD} />));
    await screen.findAllByRole("checkbox");
    const views = Array.from(document.querySelectorAll<HTMLButtonElement>(".receipts-bar .row:nth-child(2) button"));
    fireEvent.click(views[1]);
    await screen.findByRole("table");
    expect(left().filter(data)).toEqual([]);
    // The receipt with a camera and a place, wherever the sort put it.
    const thumbs = Array.from(document.querySelectorAll<HTMLButtonElement>("button.thumb"));
    fireEvent.click(thumbs.find((one) => one.closest("tr")!.textContent!.includes("2026-03-01"))!);
    await screen.findByText("Doe");
    expect(left().filter(data)).toEqual([]);
  });

  it("the import guide: no document in this language, so the English one, marked as English", () => {
    render(<ImportGuide />);
    expect(left()).toEqual([]);
    const english = document.querySelector('[lang="en"]') as HTMLElement;
    expect(english.textContent).toContain("How import works");
    expect(document.querySelector(".banner.info")!.textContent).not.toMatch(/^[\x20-\x7e]+$/);
  });
});

describe("the import guide in English", () => {
  it("is the English document exactly, with no notice and no language mark", async () => {
    await activate("en");
    const { container: picked } = render(<ImportGuide />);
    const shown = picked.innerHTML;
    cleanup();
    const { container: direct } = render(<ImportGuideDocument />);
    expect(shown).toBe(direct.innerHTML);
    expect(shown).not.toContain('lang="en"');
    expect(guideFor("en")).toBe(ImportGuideDocument);
    expect(guideFor("en-GB")).toBe(ImportGuideDocument);
    expect(guideFor("en-XA")).toBeUndefined();
    expect(guideFor("sv-SE")).toBeUndefined();
  });
});
