// @vitest-environment jsdom

/**
 * A new authenticator still owed, after a reload (#287).
 *
 * A locked member signs in with a recovery code, the sign-in screen keeps the
 * grant for this tab and shows "A new authenticator" -- and the tab reloads.
 * The app boots straight into the shell, signed in. Nothing there used to say
 * a new authenticator was owed: only the profile read the grant or asked
 * `GET /me/authenticator`, and the owners' banner is about other members. So
 * the grant went unused and the next sign-in cost a second recovery code.
 *
 * A member, not an owner, so the owners' banner cannot be what tells them.
 * Two members' grants in play, so one held for somebody else never reads as
 * covering this one.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

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
import { App } from "./App";
import { keepGrant } from "./lib/recoveryGrant";
import type { Household, User } from "./lib/types";

const SAM = {
  id: "user-sam",
  email: "sam@example.test",
  display_name: "Sam",
  role: "member",
  disabled_at: null,
} as User;

const HOUSEHOLDS = [
  { id: "household-a", name: "Sam's house", colours: null },
  { id: "household-b", name: "The flat", colours: null },
] as unknown as Household[];

/** The server after the reload: Sam signed in, and whether the key opens him. */
function server(locked: boolean) {
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === "/health") return Promise.resolve({ setup_required: false });
    if (path === "/me") return Promise.resolve(SAM);
    if (path === "/households") return Promise.resolve(HOUSEHOLDS);
    if (path === "/me/authenticator")
      return Promise.resolve({ enrolled: true, locked_by_key: locked });
    return new Promise(() => {});
  });
}

function boot() {
  const client = new QueryClient({
    defaultOptions: { queries: { refetchOnWindowFocus: false, retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
  return client;
}

const notice = () => screen.findByRole("status", { name: "Your authenticator" });

beforeEach(() => {
  window.sessionStorage.clear();
  vi.mocked(api.get).mockReset();
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});

describe("a new authenticator still owed, in the shell", () => {
  it("tells the member the grant this tab kept covers it, and opens the profile that spends it", async () => {
    keepGrant(SAM.id, "grant-sam"); // what the sign-in kept before the reload
    server(true);
    boot();

    const said = await notice();
    expect(said.textContent).toContain("your authenticator no longer works here");
    expect(said.textContent).toContain("every sign-in will ask for another recovery code");
    expect(said.textContent).toContain("The recovery code you signed in with in this tab covers it");
    expect(api.get).not.toHaveBeenCalledWith("/admin/recovery-mode");

    fireEvent.click(within(said).getByRole("button", { name: "Set up a new authenticator" }));
    expect(await screen.findByText(/covers it — no other code is needed/)).toBeTruthy();
  });

  it("says it takes a recovery code when the grant this tab holds is somebody else's", async () => {
    keepGrant("user-jane", "grant-jane");
    server(true);
    boot();

    const said = await notice();
    expect(said.textContent).toContain("Setting it up takes one of your recovery codes.");
    expect(said.textContent).not.toContain("covers it");
  });

  it("is not there for a member the key opens", async () => {
    keepGrant(SAM.id, "grant-sam");
    server(false);
    const client = boot();

    await screen.findByRole("group", { name: "Sam's house" });
    // Answered, not merely asked: nothing on screen before the answer says nothing.
    await vi.waitFor(() =>
      expect(client.getQueryState(["authenticator"])?.status).toBe("success"),
    );
    expect(screen.queryByRole("status", { name: "Your authenticator" })).toBeNull();
  });
});
