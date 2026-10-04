// @vitest-environment jsdom

/**
 * Following an account reset link (#284).
 *
 * The page says who reset the account -- an owner by name, or the server --
 * sends exactly what the link is for, and shows the new recovery codes once,
 * with the authenticator's secret gone from the screen by then.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import type { ResetState } from "../lib/types";
import { ResetAccount } from "./ResetAccount";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const BOTH: ResetState = {
  email: "partner@example.com",
  display_name: "Partner",
  password: true,
  authenticator: true,
  reset_by: "Jane",
  created_at: "2026-10-01T09:00:00",
};
const OFFER = { blob: "sealed", otpauth_uri: "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP", secret: "JBSWY3DPEHPK3PXP" };
const CODES = Array.from({ length: 10 }, (_, n) => `code${n}abcde`);

describe("a reset link", () => {
  it("names the owner, sends both, and shows the codes once", async () => {
    vi.mocked(api.get).mockResolvedValue(BOTH);
    vi.mocked(api.post).mockImplementation(async (path: string) =>
      path.endsWith("/authenticator") ? OFFER : { recovery_codes: CODES },
    );
    const done = vi.fn();
    render(<ResetAccount token="tok" onDone={done} />);

    expect((await screen.findByText(/Jane reset your password and your authenticator/)).textContent).toContain(
      "partner@example.com",
    );
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a long new password" } });
    fireEvent.change(await screen.findByLabelText("The six digits"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Check the code and finish" }));

    await vi.waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/reset/tok", {
        password: "a long new password",
        blob: "sealed",
        code: "123456",
      }),
    );
    expect(await screen.findByText(CODES[9])).toBeTruthy();
    expect(screen.queryByText(OFFER.secret)).toBeNull();

    const signIn = screen.getByRole("button", { name: "Sign in" }) as HTMLButtonElement;
    expect(signIn.disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(signIn);
    expect(done).toHaveBeenCalledTimes(1);
  });

  it("from the server, for the password only, asks for no authenticator", async () => {
    vi.mocked(api.get).mockResolvedValue({ ...BOTH, authenticator: false, reset_by: null });
    vi.mocked(api.post).mockResolvedValue({ recovery_codes: [] });
    render(<ResetAccount token="tok" onDone={vi.fn()} />);

    expect(await screen.findByText(/Your password for partner@example.com was reset from the server/)).toBeTruthy();
    expect(screen.queryByLabelText("The six digits")).toBeNull();
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a long new password" } });
    fireEvent.click(screen.getByRole("button", { name: "Set the password" }));

    await vi.waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/reset/tok", {
        password: "a long new password",
        blob: null,
        code: null,
      }),
    );
    expect(api.post).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/Your new password is set/)).toBeTruthy();
  });
});
