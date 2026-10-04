// @vitest-environment jsdom

/**
 * Signing in with a recovery code says what it took back (#210).
 *
 * Redeeming one revokes every live agent key. When it revoked any, the screen
 * holds long enough to say how many before going on; when it revoked none,
 * and on an ordinary sign-in, it goes straight on as before.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { heldGrant } from "../lib/recoveryGrant";
import type { User } from "../lib/types";
import { SignIn } from "./SignIn";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

const JANE: User = {
  id: "u1",
  email: "jane@example.com",
  display_name: "Jane",
  role: "owner",
  disabled_at: null,
};

async function recoverWith(keysRevoked: number) {
  vi.mocked(api.post).mockImplementation(async (path: string) =>
    path === "/session"
      ? { authenticated: false, needs_code: true, user: null }
      : { authenticated: true, needs_code: false, user: JANE, keys_revoked: keysRevoked },
  );
  const done = vi.fn();
  render(<SignIn onDone={done} />);
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: JANE.email } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a long password" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  fireEvent.click(await screen.findByRole("button", { name: /use a recovery code/ }));
  fireEvent.change(screen.getByLabelText("Recovery code"), { target: { value: "abcd-efgh-ij" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith("/session/recovery", { code: "abcd-efgh-ij" }));
  return done;
}

describe("a recovery code", () => {
  it("that revoked keys says how many before going on", async () => {
    const done = await recoverWith(2);
    const notice = await screen.findByRole("status");
    expect(notice.textContent).toContain("revoked 2 agent keys");
    expect(done).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(done).toHaveBeenCalledWith(JANE);
  });

  it("that revoked none goes straight on", async () => {
    const done = await recoverWith(0);
    await vi.waitFor(() => expect(done).toHaveBeenCalledWith(JANE));
    expect(screen.queryByRole("status")).toBeNull();
  });
});

/**
 * Recovery mode (#287): the server's key cannot open this member's
 * authenticator. The code step's refusal turns the screen to the recovery
 * code, and the recovery code goes straight on to a new authenticator, paid
 * for by the grant the sign-in returned -- not by a second recovery code.
 */
describe("recovery mode", () => {
  const OFFER = { token: "offer-1", secret: "JBSWY3DPEHPK3PXP", uri: "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP" };

  function replacedKey() {
    const refusal = Object.assign(new Error("this server's secret key has been replaced"), {
      status: 401,
      body: { detail: "this server's secret key has been replaced", key_replaced: true },
    });
    vi.mocked(api.post).mockImplementation(async (path: string) => {
      if (path === "/session") return { authenticated: false, needs_code: true, user: null };
      if (path === "/session/code") throw refusal;
      if (path === "/session/recovery")
        return { authenticated: true, needs_code: false, user: JANE, reenrolment_grant: "grant-1" };
      if (path === "/me/authenticator") return OFFER;
      return { devices_revoked: 0, other_sessions_ended: 0 };
    });
  }

  async function toTheCode() {
    const done = vi.fn();
    render(<SignIn onDone={done} />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: JANE.email } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a long password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    fireEvent.change(await screen.findByLabelText("Code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    return done;
  }

  const calls = (path: string) => vi.mocked(api.post).mock.calls.filter(([one]) => one === path);

  it("turns the refused code into the recovery code, and says why", async () => {
    replacedKey();
    await toTheCode();

    const said = await screen.findByRole("alert");
    expect(said.textContent).toContain("secret key has been replaced");
    expect(screen.getByLabelText("Recovery code")).toBeTruthy();
    expect(screen.queryByLabelText("Code")).toBeNull();
    // No way back to a code that cannot work here.
    expect(screen.queryByRole("button", { name: /authenticator after all/ })).toBeNull();
  });

  it("goes from one recovery code to a new authenticator, proved by the grant", async () => {
    replacedKey();
    const done = await toTheCode();
    fireEvent.change(await screen.findByLabelText("Recovery code"), { target: { value: "aaaaa11111" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await screen.findByText("A new authenticator");
    expect(calls("/me/authenticator")).toEqual([
      ["/me/authenticator", { current_password: "a long password" }],
    ]);
    expect(done).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("The six digits"), { target: { value: "654321" } });
    fireEvent.click(screen.getByRole("button", { name: "Check the code and finish" }));

    await vi.waitFor(() => expect(done).toHaveBeenCalledWith(JANE));
    expect(calls("/me/authenticator/confirm")).toEqual([
      ["/me/authenticator/confirm", { token: "offer-1", code: "654321", grant: "grant-1" }],
    ]);
    expect(calls("/session/recovery")).toHaveLength(1);
    expect(heldGrant(JANE.id)).toBeNull();
  });

  it("keeps the grant for this member while the new authenticator waits", async () => {
    replacedKey();
    await toTheCode();
    fireEvent.change(await screen.findByLabelText("Recovery code"), { target: { value: "aaaaa11111" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await screen.findByText("A new authenticator");
    expect(heldGrant(JANE.id)).toBe("grant-1");
    expect(heldGrant("somebody-else")).toBeNull();
  });

  it("can be put off, and then sets nothing up", async () => {
    replacedKey();
    const done = await toTheCode();
    fireEvent.change(await screen.findByLabelText("Recovery code"), { target: { value: "aaaaa11111" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    fireEvent.click(await screen.findByRole("button", { name: "Not now" }));
    expect(done).toHaveBeenCalledWith(JANE);
    expect(calls("/me/authenticator/confirm")).toEqual([]);
    expect(heldGrant(JANE.id)).toBe("grant-1");
  });

  it("goes straight to the recovery code when the password step says so", async () => {
    const SENTENCE = "this server's secret key has been replaced, so it cannot check a code";
    vi.mocked(api.post).mockImplementation(async (path: string) => {
      if (path === "/session")
        return {
          authenticated: false,
          needs_code: true,
          user: null,
          key_replaced: true,
          detail: SENTENCE,
        };
      if (path === "/session/recovery")
        return { authenticated: true, needs_code: false, user: JANE };
      throw new Error(`unexpected POST ${path}`);
    });
    const done = vi.fn();
    render(<SignIn onDone={done} />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: JANE.email } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a long password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByLabelText("Recovery code")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toBe(SENTENCE);
    expect(screen.queryByLabelText("Code")).toBeNull();
    expect(screen.queryByRole("button", { name: /authenticator after all/ })).toBeNull();

    fireEvent.change(screen.getByLabelText("Recovery code"), { target: { value: "aaaaa11111" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await vi.waitFor(() => expect(done).toHaveBeenCalledWith(JANE));
    expect(calls("/session/code")).toEqual([]);
  });

  it("asks a member the key opens for the authenticator code, as ever", async () => {
    vi.mocked(api.post).mockResolvedValue({
      authenticated: false,
      needs_code: true,
      user: null,
      key_replaced: false,
      detail: null,
    });
    render(<SignIn onDone={() => {}} />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: JANE.email } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a long password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByLabelText("Code")).toBeTruthy();
    expect(screen.queryByLabelText("Recovery code")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("signs in and re-enrols in a browser that refuses to store anything", async () => {
    vi.spyOn(window, "sessionStorage", "get").mockImplementation(() => {
      throw new DOMException("The operation is insecure.", "SecurityError");
    });
    replacedKey();
    const done = await toTheCode();
    fireEvent.change(await screen.findByLabelText("Recovery code"), { target: { value: "aaaaa11111" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await screen.findByText("A new authenticator");
    expect(heldGrant(JANE.id)).toBeNull();
    fireEvent.change(screen.getByLabelText("The six digits"), { target: { value: "654321" } });
    fireEvent.click(screen.getByRole("button", { name: "Check the code and finish" }));

    await vi.waitFor(() => expect(done).toHaveBeenCalledWith(JANE));
    expect(calls("/me/authenticator/confirm")).toEqual([
      ["/me/authenticator/confirm", { token: "offer-1", code: "654321", grant: "grant-1" }],
    ]);
  });
});
