// @vitest-environment jsdom

/**
 * Resetting somebody's sign-in from Admin, and the lists that make it seen
 * (#286).
 *
 * What is pinned is what reaches the API and what the screen keeps: nothing
 * is sent until at least one switch is chosen, exactly one request with those
 * switches when it is, the link shown once and gone after, and the two lists
 * sorting at their headings. Two owners and two members, so every request has
 * a wrong person it could have been aimed at.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { NOTICE_REFRESH_MS, SignInNotice, noticeKey } from "../components/SignInNotice";
import type { AdminUser, PendingReset, SignInChange, User } from "../lib/types";
import { Admin } from "./Admin";

function person(id: string, name: string, over: Partial<AdminUser> = {}): AdminUser {
  return {
    id,
    email: `${id}@example.test`,
    display_name: name,
    role: "member",
    created_at: "2026-09-01T00:00:00Z",
    disabled_at: null,
    recovery_codes_left: 10,
    households: [],
    ...over,
  };
}

const USERS = [
  person("u-jane", "Jane", { role: "owner" }),
  person("u-alex", "Alex", { role: "owner" }),
  person("u-sam", "Sam"),
  person("u-robin", "Robin", { disabled_at: "2026-09-20T00:00:00Z" }),
];
const ME = { id: "u-jane", email: "u-jane@example.test", display_name: "Jane", role: "owner" } as User;

const PENDING: PendingReset[] = [
  {
    id: "r-1",
    user_id: "u-sam",
    display_name: "Sam",
    email: "u-sam@example.test",
    password: true,
    authenticator: false,
    issued_by: "Jane",
    created_at: "2026-09-30T10:00:00",
    expires_at: "2026-10-03T10:00:00",
    expired: false,
  },
  {
    id: "r-2",
    user_id: "u-alex",
    display_name: "Alex",
    email: "u-alex@example.test",
    password: false,
    authenticator: true,
    issued_by: null,
    created_at: "2026-09-20T10:00:00",
    expires_at: "2026-09-23T10:00:00",
    expired: true,
  },
];

function change(over: Partial<SignInChange>): SignInChange {
  const id = over.id ?? 1;
  return {
    id,
    key: `${id}:${over.what ?? "reset"}`,
    what: "reset",
    user_id: "u-sam",
    user_name: "Sam",
    password: true,
    authenticator: true,
    by_id: "u-jane",
    by_name: "Jane",
    from_server: false,
    at: "2026-09-30T10:00:00",
    ...over,
  };
}

const CHANGES: SignInChange[] = [
  change({ id: 30, what: "promoted", user_id: "u-robin", user_name: "Robin", by_id: null, by_name: null, from_server: true, password: false, authenticator: false, at: "2026-09-30T12:00:00" }),
  change({ id: 20, what: "reset", by_id: "u-alex", by_name: "Alex", at: "2026-09-29T12:00:00" }),
  change({ id: 10, what: "owner_added", user_id: "u-alex", user_name: "Alex", password: false, authenticator: false, at: "2026-09-25T12:00:00" }),
];

const ISSUED = {
  link: "https://example.test/reset/once-only-token",
  expires_at: "2026-10-04T09:00:00",
  password: true,
  authenticator: false,
};

beforeEach(() => {
  vi.mocked(api.get).mockImplementation(((path: string) => {
    if (path === "/admin/users") return Promise.resolve(USERS);
    if (path === "/admin/resets") return Promise.resolve(PENDING);
    if (path === "/admin/sign-in-changes") return Promise.resolve(CHANGES);
    return Promise.resolve([]);
  }) as typeof api.get);
  vi.mocked(api.post).mockResolvedValue(ISSUED);
  vi.mocked(api.del).mockResolvedValue(null);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  try {
    window.localStorage.clear();
  } catch {
    /* nothing to clear */
  }
});

async function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Admin user={ME} />
    </QueryClientProvider>,
  );
  await screen.findByRole("table", { name: "Pending reset links" });
  await screen.findByRole("table", { name: "Recent sign-in changes" });
}

function bodyRows(table: HTMLElement): string[][] {
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent ?? ""));
}

describe("the reset dialog", () => {
  it("sends nothing until a switch is chosen, then exactly that, for that person", async () => {
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Reset sign-in for Sam" }));
    const dialog = screen.getByRole("dialog", { name: "Reset sign-in for Sam?" });
    const confirm = within(dialog).getByRole("button", { name: "Reset and make the link" });

    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    expect(dialog.textContent).toContain("Choose at least one.");
    fireEvent.click(confirm);
    expect(api.post).not.toHaveBeenCalled();

    // Sam already has a link pending: the dialog says this one replaces it.
    expect(dialog.textContent).toContain("already pending");

    fireEvent.click(within(dialog).getByRole("checkbox", { name: "Password" }));
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
    expect(dialog.textContent).toContain("their password stops working");
    expect(dialog.textContent).not.toContain("recovery codes are cleared");
    expect(dialog.textContent).toContain("signed out everywhere");

    // Both, then one taken back off: what is sent is what is ticked.
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /^Authenticator/ }));
    expect(dialog.textContent).toContain("their authenticator and recovery codes are cleared");
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /^Authenticator/ }));

    fireEvent.click(confirm);
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/admin/users/u-sam/reset", {
      password: true,
      authenticator: false,
    });
  });

  it("shows the link once, with its expiry, and it is gone after Done", async () => {
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Reset sign-in for Sam" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Password" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset and make the link" }));

    const shown = await screen.findByRole("dialog", { name: "Reset link for Sam" });
    const field = within(shown).getByLabelText("Reset link") as HTMLInputElement;
    expect(field.value).toBe(ISSUED.link);
    expect(shown.textContent).toContain("Expires");
    expect(shown.textContent).toContain("it is not sent anywhere");

    fireEvent.click(within(shown).getByRole("button", { name: "Copy" }));
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith(ISSUED.link));
    await within(shown).findByRole("button", { name: "Copied" });
    fireEvent.click(within(shown).getByRole("button", { name: "Done" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByDisplayValue(ISSUED.link)).toBeNull();
    // Opening it again starts a new reset; it never shows the old link.
    fireEvent.click(screen.getByRole("button", { name: "Reset sign-in for Sam" }));
    expect(screen.getByRole("dialog", { name: "Reset sign-in for Sam?" })).toBeTruthy();
    expect(screen.queryByDisplayValue(ISSUED.link)).toBeNull();
    expect(api.post).toHaveBeenCalledTimes(1);
  });

  it("cannot be closed or sent twice while the reset is in flight, and then shows the link", async () => {
    // The server has shut Sam's account by the time it answers, and the
    // answer is the only copy of the link: closing first would lose it.
    let answer: (made: typeof ISSUED) => void = () => undefined;
    vi.mocked(api.post).mockImplementation(
      () => new Promise((resolve) => (answer = resolve as (made: typeof ISSUED) => void)),
    );
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Reset sign-in for Sam" }));
    const asking = screen.getByRole("dialog", { name: "Reset sign-in for Sam?" });
    fireEvent.click(within(asking).getByRole("checkbox", { name: "Password" }));
    const confirm = within(asking).getByRole("button", { name: "Reset and make the link" });

    // In the same tick as the click, before the render knows it is pending:
    // a double click, and Escape.
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    fireEvent.keyDown(document, { key: "Escape" });
    await within(asking).findByRole("button", { name: "Resetting…" });
    expect((within(asking).getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(true);

    // And every way out once it is.
    fireEvent.keyDown(document, { key: "Escape" });
    fireEvent.click(within(asking).getByRole("button", { name: "Close" }));
    fireEvent.click(document.querySelector(".dialog-backdrop") as HTMLElement);
    expect(screen.getByRole("dialog", { name: "Reset sign-in for Sam?" })).toBe(asking);
    expect(api.post).toHaveBeenCalledTimes(1);
    expect(api.post).toHaveBeenCalledWith("/admin/users/u-sam/reset", {
      password: true,
      authenticator: false,
    });

    answer(ISSUED);
    const shown = await screen.findByRole("dialog", { name: "Reset link for Sam" });
    expect((within(shown).getByLabelText("Reset link") as HTMLInputElement).value).toBe(ISSUED.link);
  });

  it("closes as before once a reset has failed", async () => {
    vi.mocked(api.post).mockRejectedValue(new Error("the server said no"));
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Reset sign-in for Alex" }));
    const asking = screen.getByRole("dialog", { name: "Reset sign-in for Alex?" });
    fireEvent.click(within(asking).getByRole("checkbox", { name: /^Authenticator/ }));
    fireEvent.click(within(asking).getByRole("button", { name: "Reset and make the link" }));
    expect((await within(asking).findByRole("alert")).textContent).toContain("the server said no");
    expect(api.post).toHaveBeenCalledWith("/admin/users/u-alex/reset", {
      password: false,
      authenticator: true,
    });

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("is not offered for yourself or for a disabled account", async () => {
    await mount();
    const mine = screen.getByRole("button", { name: "Reset sign-in for Jane" }) as HTMLButtonElement;
    const disabled = screen.getByRole("button", { name: "Reset sign-in for Robin" }) as HTMLButtonElement;
    const otherOwner = screen.getByRole("button", { name: "Reset sign-in for Alex" }) as HTMLButtonElement;
    expect([mine.disabled, disabled.disabled, otherOwner.disabled]).toEqual([true, true, false]);
  });
});

describe("pending reset links", () => {
  it("sort at their headings and mark the lapsed one", async () => {
    await mount();
    const table = screen.getByRole("table", { name: "Pending reset links" });
    // Newest first to begin with.
    expect(bodyRows(table).map((cells) => cells[0])).toEqual([
      "Sam u-sam@example.test",
      "Alex u-alex@example.test",
    ]);
    const lapsed = bodyRows(table)[1];
    expect(lapsed[2]).toBe("from the server");
    expect(lapsed[4]).toContain("expired");
    expect(bodyRows(table)[0][4]).not.toContain("expired");

    fireEvent.click(within(table).getByRole("button", { name: "Who" }));
    expect(bodyRows(table).map((cells) => cells[1])).toEqual(["authenticator", "password"]);
    fireEvent.click(within(table).getByRole("button", { name: "Who" }));
    expect(bodyRows(table).map((cells) => cells[1])).toEqual(["password", "authenticator"]);

    fireEvent.click(within(table).getByRole("button", { name: "Expires" }));
    expect(bodyRows(table)[0][0]).toContain("Alex");
  });

  it("withdraws only the link it named, after asking", async () => {
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Withdraw the reset link for Alex" }));
    expect(api.del).not.toHaveBeenCalled();
    const confirm = screen.getByRole("dialog", { name: "Withdraw this reset link?" });
    expect(confirm.textContent).toContain("u-alex@example.test");
    fireEvent.click(within(confirm).getByRole("button", { name: "Yes, withdraw it" }));
    await waitFor(() => expect(api.del).toHaveBeenCalledTimes(1));
    expect(api.del).toHaveBeenCalledWith("/admin/resets/r-2");
  });
});

describe("recent sign-in changes", () => {
  it("say who did each, the server included, and sort at their headings", async () => {
    await mount();
    const table = screen.getByRole("table", { name: "Recent sign-in changes" });
    const rows = bodyRows(table);
    expect(rows.map((cells) => cells.slice(1))).toEqual([
      ["Made an owner", "Robin", "from the server"],
      ["Reset: password and authenticator", "Sam", "Alex"],
      ["Added as an owner", "Alex", "Jane"],
    ]);

    fireEvent.click(within(table).getByRole("button", { name: "Whom" }));
    expect(bodyRows(table).map((cells) => cells[2])).toEqual(["Alex", "Robin", "Sam"]);
    fireEvent.click(within(table).getByRole("button", { name: "By" }));
    expect(bodyRows(table).map((cells) => cells[3])).toEqual(["Alex", "from the server", "Jane"]);
    fireEvent.click(within(table).getByRole("button", { name: "What" }));
    expect(bodyRows(table).map((cells) => cells[1])).toEqual([
      "Added as an owner",
      "Made an owner",
      "Reset: password and authenticator",
    ]);
    fireEvent.click(within(table).getByRole("button", { name: "What" }));
    expect(bodyRows(table).map((cells) => cells[1])).toEqual([
      "Reset: password and authenticator",
      "Made an owner",
      "Added as an owner",
    ]);
  });
});

describe("the notice in the shell", () => {
  function shell(viewer: User, items: SignInChange[]) {
    vi.mocked(api.get).mockResolvedValue(items);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const opened = vi.fn();
    render(
      <QueryClientProvider client={client}>
        <SignInNotice user={viewer} onOpen={opened} />
      </QueryClientProvider>,
    );
    return opened;
  }

  const ALEX = { id: "u-alex", email: "u-alex@example.test", display_name: "Alex", role: "owner" } as User;

  it("tells an owner what somebody else did, and remembers the dismissal for that owner", async () => {
    const mine = change({ id: 40, user_name: "Robin", user_id: "u-robin" }); // Jane's own
    shell(ME, [mine, ...CHANGES]);
    const notice = await screen.findByRole("status", { name: "Recent sign-in changes" });
    expect(notice.textContent).toContain("The server made Robin an owner.");
    expect(notice.textContent).toContain("Alex reset Sam's password and authenticator.");
    // Jane's own acts are not news to Jane.
    expect(notice.textContent).not.toContain("Jane reset Robin's");
    expect(notice.textContent).not.toContain("Jane added Alex as an owner");

    fireEvent.click(within(notice).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("status")).toBeNull();
    // The newest of what she dismissed, by position in the log and by time.
    expect(JSON.parse(window.localStorage.getItem(noticeKey("u-jane")) ?? "null")).toEqual({
      seq: 30,
      at: "2026-09-30T12:00:00",
    });

    // Back again: still dismissed for Jane, and a newer change brings it back.
    cleanup();
    shell(ME, CHANGES);
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(screen.queryByRole("status")).toBeNull();
    cleanup();
    // Later in the log, though its batch started before the one dismissed:
    // the position alone is enough to be news.
    shell(ME, [
      change({ id: 50, what: "promoted", by_id: "u-alex", by_name: "Alex", password: false, authenticator: false }),
      ...CHANGES,
    ]);
    const again = await screen.findByRole("status", { name: "Recent sign-in changes" });
    expect(again.textContent).toContain("Alex made Sam an owner.");
    expect(again.textContent).not.toContain("The server made Robin an owner.");

    // Another owner on the same browser has dismissed nothing.
    cleanup();
    shell(ALEX, CHANGES);
    const theirs = await screen.findByRole("status", { name: "Recent sign-in changes" });
    expect(theirs.textContent).toContain("The server made Robin an owner.");
    expect(theirs.textContent).toContain("Jane added Alex as an owner.");
    expect(theirs.textContent).not.toContain("Alex reset Sam's");
  });

  it("still tells an owner what happens after a restore rewound the log", async () => {
    // Before the restore: Jane dismisses everything, up to position 30.
    shell(ME, CHANGES);
    const before = await screen.findByRole("status", { name: "Recent sign-in changes" });
    fireEvent.click(within(before).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("status")).toBeNull();

    // The backup ended at position 20, so 30 is gone and the log carries on
    // from 21. Two days later Alex resets Robin, and the server gives Sam a
    // new authenticator: positions 24 and 25, both below Jane's mark.
    const restored = CHANGES.filter((one) => one.id <= 20);
    const since = [
      change({ id: 25, what: "authenticator_replaced", user_id: "u-sam", user_name: "Sam", by_id: null, by_name: null, from_server: true, password: false, authenticator: true, at: "2026-10-02T09:30:00" }),
      change({ id: 24, user_id: "u-robin", user_name: "Robin", by_id: "u-alex", by_name: "Alex", password: true, authenticator: false, at: "2026-10-02T09:00:00" }),
    ];
    cleanup();
    shell(ME, [...since, ...restored]);
    const after = await screen.findByRole("status", { name: "Recent sign-in changes" });
    expect(after.textContent).toContain("Alex reset Robin's password.");
    expect(after.textContent).toContain("The server gave Sam a new authenticator.");
    // What she dismissed before the restore, and the backup kept, stays
    // dismissed.
    expect(after.textContent).not.toContain("Alex reset Sam's");

    // Dismissed again, the mark is the newest of these two.
    fireEvent.click(within(after).getByRole("button", { name: "Dismiss" }));
    expect(JSON.parse(window.localStorage.getItem(noticeKey("u-jane")) ?? "null")).toEqual({
      seq: 25,
      at: "2026-10-02T09:30:00",
    });

    // Alex, on the same browser, is told only what the server did.
    cleanup();
    shell(ALEX, [...since, ...restored]);
    const theirs = await screen.findByRole("status", { name: "Recent sign-in changes" });
    expect(theirs.textContent).toContain("The server gave Sam a new authenticator.");
    expect(theirs.textContent).not.toContain("Alex reset Robin's");
  });

  it("asks again while it stays mounted: on a timer, and when the tab comes back", async () => {
    // The shell keeps the notice mounted for the whole sign-in, under
    // main.tsx's defaults, which never refetch on focus.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let served: SignInChange[] = [];
      vi.mocked(api.get).mockImplementation((() => Promise.resolve(served)) as typeof api.get);
      const client = new QueryClient({
        defaultOptions: { queries: { refetchOnWindowFocus: false, retry: false } },
      });
      render(
        <QueryClientProvider client={client}>
          <SignInNotice user={ME} onOpen={vi.fn()} />
        </QueryClientProvider>,
      );
      await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
      expect(screen.queryByRole("status")).toBeNull();

      // Alex resets Sam while Jane is somewhere else in the app.
      served = [change({ id: 60, by_id: "u-alex", by_name: "Alex" })];
      await vi.advanceTimersByTimeAsync(NOTICE_REFRESH_MS);
      const notice = await screen.findByRole("status", { name: "Recent sign-in changes" });
      expect(notice.textContent).toContain("Alex reset Sam's password and authenticator.");
      expect(api.get).toHaveBeenCalledTimes(2);
      expect(api.get).toHaveBeenLastCalledWith("/admin/sign-in-changes");

      // Then the server makes Robin an owner while the tab is in the
      // background; it is asked again the moment the tab is back.
      served = [
        change({ id: 61, what: "promoted", user_id: "u-robin", user_name: "Robin", by_id: null, by_name: null, from_server: true, password: false, authenticator: false }),
        ...served,
      ];
      window.dispatchEvent(new Event("visibilitychange"));
      await waitFor(() =>
        expect(screen.getByRole("status").textContent).toContain("The server made Robin an owner."),
      );
      expect(api.get).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("says nothing to a member and asks the server nothing", () => {
    const member = { ...ME, id: "u-sam", role: "member" } as User;
    shell(member, CHANGES);
    expect(screen.queryByRole("status")).toBeNull();
    expect(api.get).not.toHaveBeenCalled();
  });

  it("still shows when the browser refuses storage", async () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    try {
      shell(ME, CHANGES);
      const notice = await screen.findByRole("status", { name: "Recent sign-in changes" });
      fireEvent.click(within(notice).getByRole("button", { name: "Dismiss" }));
      expect(screen.queryByRole("status")).toBeNull();
    } finally {
      getItem.mockRestore();
      setItem.mockRestore();
    }
  });
});
