// @vitest-environment jsdom

/**
 * A password opens the desktop app; one recovery code replaces it (#320).
 *
 * Pinned: what reaches the API and what the screen shows. On the desktop the
 * wizard never asks for a token or an authenticator and never calls the
 * enrol step; sign-in offers the recovery code for a forgotten password; and a
 * step-up takes the password alone. On a server, each is as it was.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("./lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
  setUnauthorizedHandler: vi.fn(),
}));

import { api } from "./lib/api";
import { EVERYTHING, setCapabilities, type Capabilities } from "./lib/capabilities";
import { NO_PROOF, StepUpFields, stepUpReady } from "./components/StepUp";
import { Setup } from "./screens/Setup";
import { SignIn } from "./screens/SignIn";
import type { User } from "./lib/types";

const DESKTOP: Capabilities = {
  ...EVERYTHING,
  snap: false,
  agents: false,
  invitations: false,
  account_resets: false,
  passkeys: false,
  second_factor: false,
  password_reset_by_code: true,
  self_update: false,
  db_view: false,
};

const JANE = { id: "u-jane", email: "jane@example.test", display_name: "Jane", role: "owner" } as User;

function wrap(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

const posted = () => vi.mocked(api.post).mock.calls.map(([path, body]) => [path, body]);

afterEach(() => {
  cleanup();
  setCapabilities(null);
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
});

describe("the setup wizard", () => {
  it("on the desktop: you, then one recovery code, and no token or authenticator", async () => {
    setCapabilities(DESKTOP);
    vi.mocked(api.get).mockResolvedValue({ setup_required: true, token_needed: false, token_path: null });
    vi.mocked(api.post).mockImplementation(async (path: string) => {
      if (path === "/setup/begin") return { blob: "b1", otpauth_uri: null, secret: null, recovery_codes: ["c0ffee1234"] };
      if (path === "/setup/complete") return JANE;
      throw new Error(`unexpected ${path}`);
    });
    const done = vi.fn();
    wrap(<Setup onDone={done} />);

    expect(screen.getByRole("heading", { name: "1. You" })).toBeTruthy();
    expect(screen.queryByLabelText("Setup token")).toBeNull();
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "jane@example.test" } });
    fireEvent.change(screen.getByLabelText("Your name"), { target: { value: "Jane" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a sufficiently long password" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await screen.findByRole("heading", { name: "2. Your recovery code" });
    expect(screen.getByText("c0ffee1234")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("I have stored it somewhere that is not this computer"));
    fireEvent.click(screen.getByRole("button", { name: "Finish setup" }));
    await vi.waitFor(() => expect(done).toHaveBeenCalledWith(JANE));

    expect(posted()).toEqual([
      ["/setup/begin", { token: "", email: "jane@example.test", display_name: "Jane", password: "a sufficiently long password" }],
      ["/setup/complete", { blob: "b1", codes_saved: true }],
    ]);
  });

  it("on a server: the token comes first, as it always has", () => {
    setCapabilities(EVERYTHING);
    vi.mocked(api.get).mockResolvedValue({ setup_required: true, token_needed: true, token_path: "/data/setup-token" });
    wrap(<Setup onDone={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "1. The setup token" })).toBeTruthy();
    expect(screen.getByLabelText("Setup token")).toBeTruthy();
  });
});

describe("a forgotten password", () => {
  it("on the desktop: the recovery code and a new password sign in, and the new code is shown", async () => {
    setCapabilities(DESKTOP);
    vi.mocked(api.post).mockImplementation(async (path: string) => {
      if (path === "/session/recover") return { user: JANE, recovery_code: "fresh00001" };
      throw new Error(`unexpected ${path}`);
    });
    const done = vi.fn();
    wrap(<SignIn onDone={done} />);

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "jane@example.test" } });
    fireEvent.click(screen.getByRole("button", { name: "Forgot your password? Use your recovery code" }));
    expect((screen.getByLabelText("Email") as HTMLInputElement).value).toBe("jane@example.test");
    fireEvent.change(screen.getByLabelText("Recovery code"), { target: { value: "c0ffee1234" } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "another long enough password" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password and sign in" }));

    await screen.findByText("fresh00001");
    expect(done).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("I have stored it somewhere that is not this computer"));
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(done).toHaveBeenCalledWith(JANE);
    expect(posted()).toEqual([
      ["/session/recover", { email: "jane@example.test", code: "c0ffee1234", new_password: "another long enough password" }],
    ]);
  });

  it("on a server: not offered", () => {
    setCapabilities(EVERYTHING);
    wrap(<SignIn onDone={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Forgot your password? Use your recovery code" })).toBeNull();
  });
});

describe("a step-up", () => {
  const withPasswordOnly = { ...NO_PROOF, password: "a sufficiently long password" };

  it("on the desktop: the password alone, and no code field", () => {
    setCapabilities(DESKTOP);
    expect(stepUpReady(withPasswordOnly)).toBe(true);
    wrap(<StepUpFields proof={withPasswordOnly} onChange={vi.fn()} why="This lasts." />);
    expect(screen.queryByLabelText("The six digits from your authenticator")).toBeNull();
    expect(screen.getByText("This lasts. So this asks for your password again.")).toBeTruthy();
  });

  it("on a server: the password and the code", () => {
    setCapabilities(EVERYTHING);
    expect(stepUpReady(withPasswordOnly)).toBe(false);
    expect(stepUpReady({ ...withPasswordOnly, code: "123456" })).toBe(true);
    wrap(<StepUpFields proof={withPasswordOnly} onChange={vi.fn()} why="This lasts." />);
    expect(screen.getByLabelText("The six digits from your authenticator")).toBeTruthy();
  });
});
