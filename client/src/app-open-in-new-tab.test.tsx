// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

vi.mock("./lib/api", () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
    upload: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
  setUnauthorizedHandler: () => {},
}));

import { api } from "./lib/api";
import { App, firstScreen, openedAt, putTheAddressBack, sectionAt } from "./App";
import type { Household, User } from "./lib/types";

describe("opening a screen in a new tab", () => {
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("opens the named screen and household, then puts the address back", () => {
    window.history.replaceState(null, "", "/?open=history&household=hh-2");
    const opened = openedAt();
    expect(opened).toEqual({ screen: "history", household: "hh-2" });
    putTheAddressBack(opened);
    expect(window.location.search).toBe("");
  });

  it("gives the same answer when asked twice, as Strict Mode asks it (#110)", () => {
    window.history.replaceState(null, "", "/?open=accounts&household=hh-2");
    expect(openedAt()).toEqual({ screen: "accounts", household: "hh-2" });
    expect(openedAt()).toEqual({ screen: "accounts", household: "hh-2" });
    expect(window.location.search).toBe("?open=accounts&household=hh-2");
  });

  it("opens the payee rules, which the One-time Import's report points at (#265)", () => {
    window.history.replaceState(null, "", "/?open=rules&household=hh-2");
    expect(openedAt()).toEqual({ screen: "rules", household: "hh-2" });
  });

  it("opens Application management, where the Updating panel sends the page once the app is back", () => {
    window.history.replaceState(null, "", "/?open=application");
    const opened = openedAt();
    expect(opened).toEqual({ screen: "application", household: "" });
    putTheAddressBack(opened);
    expect(window.location.search).toBe("");
  });

  it("ignores a screen it was not built to open", () => {
    window.history.replaceState(null, "", "/?open=settings&household=hh-2");
    const opened = openedAt();
    expect(opened).toEqual({ screen: null, household: "" });
    putTheAddressBack(opened);
    expect(window.location.search).toBe("?open=settings&household=hh-2");
  });

  it("drops a household that is not an id (#197)", () => {
    for (const bad of ["../me", "..%2Fme%3Fx%3D", "a/b", "x".repeat(65)]) {
      window.history.replaceState(null, "", `/?open=accounts&household=${bad}`);
      expect(openedAt()).toEqual({ screen: "accounts", household: "" });
    }
  });
});

describe("the screen the shell opens on", () => {
  it("is the register when nothing was asked for", () => {
    expect(firstScreen({ screen: null }, "owner")).toBe("register");
    expect(firstScreen({ screen: null }, "member")).toBe("register");
  });

  it("is Application management for an owner, and the register for a member", () => {
    expect(firstScreen({ screen: "application" }, "owner")).toBe("application");
    expect(firstScreen({ screen: "application" }, "member")).toBe("register");
  });

  it("is a member's own screen for a member", () => {
    expect(firstScreen({ screen: "history" }, "member")).toBe("history");
  });
});

describe("the section the opened screen scrolls to", () => {
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("is Updates when the Updating panel asks for it", () => {
    window.history.replaceState(null, "", "/?open=application#updates");
    expect(sectionAt(openedAt())).toBe("updates");
  });

  it("is nothing without the #, on another screen, or for another #", () => {
    for (const address of ["/?open=application", "/?open=history#updates", "/#updates", "/?open=application#logs"]) {
      window.history.replaceState(null, "", address);
      expect(sectionAt(openedAt())).toBeNull();
    }
  });

  it("goes with the rest of the address once read", () => {
    window.history.replaceState(null, "", "/?open=application#updates");
    putTheAddressBack(openedAt());
    expect(window.location.hash).toBe("");
    expect(window.location.search).toBe("");
  });
});

// --------------------------------------------------------------------------- //
// The whole shell, signed in as an owner and as a member
// --------------------------------------------------------------------------- //

const HOUSEHOLDS = [
  { id: "household-a", name: "Alice's house", colours: null },
  { id: "household-b", name: "The flat", colours: null },
] as unknown as Household[];

function person(role: User["role"]): User {
  return {
    id: role === "owner" ? "user-a" : "user-b",
    email: role === "owner" ? "a@example.test" : "b@example.test",
    display_name: role === "owner" ? "Alice" : "Bob",
    role,
    disabled_at: null,
  } as User;
}

function boot(search: string, who: User) {
  window.history.replaceState(null, "", search);
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === "/health") return Promise.resolve({ setup_required: false });
    if (path === "/me") return Promise.resolve(who);
    if (path === "/households") return Promise.resolve(HOUSEHOLDS);
    return new Promise(() => {});
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}

const paths = () => vi.mocked(api.get).mock.calls.map(([path]) => path);

describe("?open=application", () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset();
  });
  afterEach(() => {
    cleanup();
    window.history.replaceState(null, "", "/");
    window.localStorage.clear();
  });

  it("opens Application management for an owner", { timeout: 15_000 }, async () => {
    boot("/?open=application", person("owner"));
    await screen.findByText("Reading this instance…", undefined, { timeout: 5_000 });
    expect(paths()).toContain("/admin/application");
    await waitFor(() => expect(document.title).toBe("Spend Tracker - Application management - Alice's house"));
    expect(window.location.search).toBe("");
  });

  it("opens the register for a member, and asks nothing of the instance", { timeout: 15_000 }, async () => {
    boot("/?open=application", person("member"));
    await screen.findByRole("group", { name: "Alice's house" }, { timeout: 5_000 });
    await waitFor(() => expect(document.title).toBe("Spend Tracker - Transactions - Alice's house"));
    expect(screen.queryByText("Reading this instance…")).toBeNull();
    expect(paths().filter((path) => path.startsWith("/admin/"))).toEqual([]);
    expect(window.location.search).toBe("");
  });
});
