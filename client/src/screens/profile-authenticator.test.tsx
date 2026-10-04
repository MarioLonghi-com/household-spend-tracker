// @vitest-environment jsdom

/**
 * Setting up a new authenticator from the profile in recovery mode (#287).
 *
 * The grant a recovery sign-in returns is kept for this tab, so a member who
 * said "Not now" -- or reloaded -- spends it here instead of a second recovery
 * code. It is sent only while the server says the key cannot open this
 * member, and a grant the server refuses is dropped for a recovery code.
 * Two members throughout, so a grant kept for one is never sent for the other.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { heldGrant, keepGrant } from "../lib/recoveryGrant";
import type { User } from "../lib/types";
import { Profile } from "./Profile";
import { SignIn } from "./SignIn";

const JANE: User = {
  id: "u1",
  email: "jane@example.com",
  display_name: "Jane",
  role: "owner",
  disabled_at: null,
};
const SAM: User = {
  id: "u2",
  email: "sam@example.com",
  display_name: "Sam",
  role: "member",
  disabled_at: null,
};
const OFFER = {
  token: "offer-1",
  secret: "JBSWY3DPEHPK3PXP",
  uri: "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP",
};
const DONE = { devices_revoked: 0, other_sessions_ended: 0 };

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
});

/** The server: this member's status, and a re-enrolment that answers `confirm`. */
function server(locked: boolean, confirm: () => Promise<unknown> = async () => DONE) {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path === "/me/authenticator") return { enrolled: true, locked_by_key: locked };
    if (path === "/me/keys") return [];
    if (path === "/me/recovery-codes") return { unused: 9 };
    throw new Error(`unexpected GET ${path}`);
  });
  vi.mocked(api.post).mockImplementation(async (path: string) => {
    if (path === "/me/authenticator") return OFFER;
    if (path === "/me/authenticator/confirm") return confirm();
    throw new Error(`unexpected POST ${path}`);
  });
}

function profileFor(user: User) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Profile user={user} households={[]} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

/** Password, the offer, the six digits from the new authenticator. */
async function toTheOffer() {
  fireEvent.change(screen.getAllByLabelText("Current password")[1], {
    target: { value: "a long password" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Set up a new authenticator" }));
  fireEvent.change(await screen.findByLabelText("The six digits from the new one"), {
    target: { value: "654321" },
  });
}

const confirms = () =>
  vi.mocked(api.post).mock.calls.filter(([path]) => path === "/me/authenticator/confirm");

describe("the profile in recovery mode", () => {
  it("spends the grant the sign-in kept, after 'Not now', and asks for no code", async () => {
    // The sign-in: password, recovery code, grant -- and "Not now".
    vi.mocked(api.post).mockImplementation(async (path: string) => {
      if (path === "/session")
        return {
          authenticated: false,
          needs_code: true,
          user: null,
          key_replaced: true,
          detail: "this server's secret key has been replaced",
        };
      if (path === "/session/recovery")
        return { authenticated: true, needs_code: false, user: JANE, reenrolment_grant: "grant-1" };
      if (path === "/me/authenticator") return OFFER;
      throw new Error(`unexpected POST ${path}`);
    });
    const done = vi.fn();
    render(<SignIn onDone={done} />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: JANE.email } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a long password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    fireEvent.change(await screen.findByLabelText("Recovery code"), {
      target: { value: "aaaaa11111" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.click(await screen.findByRole("button", { name: "Not now" }));
    expect(done).toHaveBeenCalledWith(JANE);
    cleanup();

    // Later, the profile: no field for a code, and the grant in the request.
    expect(heldGrant(JANE.id)).toBe("grant-1");
    vi.mocked(api.post).mockReset();
    server(true);
    profileFor(JANE);
    expect((await screen.findByRole("status")).textContent).toContain(
      "covers it — no other code is needed",
    );
    await toTheOffer();
    expect(screen.queryByLabelText("One of your recovery codes")).toBeNull();
    expect(screen.queryByLabelText("A code from your current authenticator")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Confirm and replace" }));

    await screen.findByText(/Your new authenticator is the only one/);
    expect(confirms()).toEqual([
      ["/me/authenticator/confirm", { token: "offer-1", code: "654321", grant: "grant-1" }],
    ]);
    expect(heldGrant(JANE.id)).toBeNull();
  });

  it("never sends one member's grant for another", async () => {
    keepGrant(JANE.id, "grant-1");
    server(true);
    profileFor(SAM);
    await toTheOffer();
    fireEvent.change(screen.getByLabelText("One of your recovery codes"), {
      target: { value: "bbbbb22222" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm and replace" }));

    await vi.waitFor(() =>
      expect(confirms()).toEqual([
        [
          "/me/authenticator/confirm",
          { token: "offer-1", code: "654321", current_code: "bbbbb22222" },
        ],
      ]),
    );
  });

  it("asks the member the key opens for their current authenticator, grant or no grant", async () => {
    keepGrant(JANE.id, "grant-1");
    server(false);
    profileFor(JANE);
    await toTheOffer();
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.change(screen.getByLabelText("A code from your current authenticator"), {
      target: { value: "123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm and replace" }));

    await vi.waitFor(() =>
      expect(confirms()).toEqual([
        ["/me/authenticator/confirm", { token: "offer-1", code: "654321", current_code: "123456" }],
      ]),
    );
  });

  it("drops a grant the server refuses and asks for a recovery code instead", async () => {
    keepGrant(JANE.id, "grant-1");
    const refusal = Object.assign(new Error("that permission has expired"), {
      status: 401,
      body: { detail: "that permission has expired" },
    });
    server(true, async () => {
      throw refusal;
    });
    profileFor(JANE);
    await screen.findByRole("status");
    await toTheOffer();
    fireEvent.click(screen.getByRole("button", { name: "Confirm and replace" }));

    expect(await screen.findByLabelText("One of your recovery codes")).toBeTruthy();
    expect((await screen.findByRole("alert")).textContent).toContain("expired");
    expect(heldGrant(JANE.id)).toBeNull();
    expect(confirms()).toHaveLength(1);
  });

  it("says what a step-up refused for a replaced key means to somebody signed in", async () => {
    // The server's step-up sentence (`KEY_REPLACED_STEP_UP`), not the sign-in's.
    const STEP_UP =
      "this server's secret key has been replaced, so it cannot check a code from your " +
      "authenticator, and a recovery code does not stand in for one here. Set up a new " +
      "authenticator from your account first; a code from it will work here.";
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path === "/me/authenticator" ? { enrolled: true, locked_by_key: true } : [],
    );
    vi.mocked(api.post).mockImplementation(async (path: string) => {
      if (path === "/me/step-up")
        throw Object.assign(new Error(STEP_UP), {
          status: 401,
          body: { detail: STEP_UP, key_replaced: true },
        });
      throw new Error(`unexpected POST ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Profile
          user={JANE}
          households={[{ id: "h1", name: "Home" } as never]}
          onClose={() => {}}
        />
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Give a program a key" }));
    fireEvent.change(screen.getByLabelText("What is it for"), { target: { value: "filer" } });
    fireEvent.change(screen.getByLabelText("Your password"), {
      target: { value: "a long password" },
    });
    fireEvent.change(screen.getByLabelText("The six digits from your authenticator"), {
      target: { value: "123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create the key" }));

    const said = await screen.findByRole("alert");
    expect(said.textContent).toContain(STEP_UP);
    expect(said.textContent).toContain("Your account is under your name in the menu.");
    // Nothing on the screen sends them to a recovery code: a step-up takes none.
    expect(said.textContent).not.toMatch(/use one of your recovery codes/i);
    expect(vi.mocked(api.post).mock.calls.map(([path]) => path)).toEqual(["/me/step-up"]);
  });
});
