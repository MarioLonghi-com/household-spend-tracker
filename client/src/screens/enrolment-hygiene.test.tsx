// @vitest-environment jsdom

/**
 * What the enrolment and sign-in screens hold and what they tell a password
 * manager (#198).
 *
 * - Once the six digits have verified, the authenticator's secret is gone
 *   from React state: step 4 shows only the recovery codes, and the secret
 *   used to stay until the page closed. Asserted by searching the rendered
 *   tree's state and props for the secret -- present at step 3 as a control,
 *   absent after.
 * - The inputs carry the `autoComplete` hints a password manager needs to
 *   offer the right thing, and to offer to save a new password.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
    upload: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { AcceptInvite } from "./AcceptInvite";
import { Setup } from "./Setup";
import { SignIn } from "./SignIn";

const SECRET = "JBSWY3DPEHPK3PXPSECRETVALUE";
const STARTED = {
  blob: "blob-1",
  otpauth_uri: `otpauth://totp/Spend%20Tracker:a@example.test?secret=${SECRET}`,
  secret: SECRET,
  recovery_codes: ["aaaa-bbbb", "cccc-dddd"],
};

afterEach(cleanup);

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.post).mockImplementation((path: string) =>
    Promise.resolve(path.endsWith("/begin") ? STARTED : { blob: "blob-2" }),
  );
});

type Fiber = {
  child: Fiber | null;
  sibling: Fiber | null;
  memoizedState: unknown;
  memoizedProps: unknown;
};

/** Every hook's state and every prop in the mounted tree, as text. */
function everythingHeld(container: HTMLElement): string {
  const rootKey = Object.keys(container).find((key) => key.startsWith("__reactContainer$"))!;
  const seen: string[] = [];
  const walk = (fiber: Fiber | null) => {
    for (let one = fiber; one; one = one.sibling) {
      // Hooks are a linked list on memoizedState; a class or host keeps a plain value.
      let hook = one.memoizedState as { memoizedState?: unknown; next?: unknown } | null;
      for (let n = 0; hook && typeof hook === "object" && n < 200; n += 1) {
        try {
          seen.push(JSON.stringify(hook.memoizedState ?? null) ?? "");
        } catch {
          /* a circular value is not a string we are looking for */
        }
        hook = (hook.next as typeof hook) ?? null;
      }
      if (typeof one.memoizedProps === "object" && one.memoizedProps) {
        for (const value of Object.values(one.memoizedProps as Record<string, unknown>))
          if (typeof value === "string") seen.push(value);
      }
      walk(one.child);
    }
  };
  // The container points at the first HostRoot fiber, which after a commit
  // may be the stale alternate; the FiberRoot's `current` is the live tree.
  const hostRoot = (container as unknown as Record<string, { stateNode: { current: Fiber } }>)[rootKey];
  walk(hostRoot.stateNode.current);
  return seen.join("\n");
}

function type(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe("the setup wizard", () => {
  it("drops the secret once the code verifies, and hints every field", async () => {
    vi.mocked(api.get).mockResolvedValue({ setup_required: true, token_path: null });
    const client = new QueryClient();
    const { container } = render(
      <QueryClientProvider client={client}>
        <Setup onDone={() => {}} />
      </QueryClientProvider>,
    );

    expect(screen.getByLabelText("Setup token").getAttribute("autocomplete")).toBe("off");
    type("Setup token", "token");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(screen.getByLabelText("Email").getAttribute("autocomplete")).toBe("username");
    expect(screen.getByLabelText("Password").getAttribute("autocomplete")).toBe("new-password");
    type("Email", "a@example.test");
    type("Your name", "Alice");
    type("Password", "correct horse battery");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await screen.findByText("3. Your authenticator");
    expect(screen.getByLabelText("The six digits").getAttribute("autocomplete")).toBe(
      "one-time-code",
    );
    expect(everythingHeld(container)).toContain(SECRET);

    type("The six digits", "123456");
    fireEvent.click(screen.getByRole("button", { name: "Check the code" }));
    await screen.findByText("4. Recovery codes");

    expect(screen.getByText("aaaa-bbbb")).toBeTruthy();
    const held = everythingHeld(container);
    expect(held).toContain("aaaa-bbbb");
    expect(held).not.toContain(SECRET);
    expect(held).not.toContain("123456");
  });
});

describe("accepting an invitation", () => {
  it("drops the secret once the code verifies, and hints every field", async () => {
    vi.mocked(api.get).mockResolvedValue({
      email: "b@example.test",
      invited_by: "Alice",
      role: "member",
      households: ["Ours"],
    });
    const { container } = render(<AcceptInvite token="t" onDone={() => {}} />);

    await screen.findByText("1. You");
    expect(screen.getByLabelText("Email").getAttribute("autocomplete")).toBe("username");
    expect(screen.getByLabelText("Password").getAttribute("autocomplete")).toBe("new-password");
    type("Your name", "Bea");
    type("Password", "correct horse battery");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await screen.findByText("2. Your authenticator");
    expect(screen.getByLabelText("The six digits").getAttribute("autocomplete")).toBe(
      "one-time-code",
    );
    expect(everythingHeld(container)).toContain(SECRET);

    type("The six digits", "654321");
    fireEvent.click(screen.getByRole("button", { name: "Check the code" }));
    await screen.findByText("3. Recovery codes");

    const held = everythingHeld(container);
    expect(held).toContain("aaaa-bbbb");
    expect(held).not.toContain(SECRET);
    expect(held).not.toContain("654321");
  });
});

describe("signing in", () => {
  it("offers a one-time code for the authenticator and nothing for a recovery code", async () => {
    vi.mocked(api.post).mockResolvedValue({ authenticated: false, needs_code: true, user: null });
    render(<SignIn onDone={() => {}} />);
    type("Email", "a@example.test");
    type("Password", "correct horse battery");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    const code = await screen.findByLabelText("Code");
    expect(code.getAttribute("autocomplete")).toBe("one-time-code");

    fireEvent.click(screen.getByRole("button", { name: /use a recovery code/ }));
    expect(screen.getByLabelText("Recovery code").getAttribute("autocomplete")).toBe("off");
  });

  it("in recovery mode, drops the password once it is spent and the secret once it verifies (#287)", async () => {
    const PASSWORD = "correct horse battery";
    vi.mocked(api.post).mockImplementation(async (path: string) => {
      if (path === "/session") return { authenticated: false, needs_code: true, user: null };
      if (path === "/session/recovery")
        return {
          authenticated: true,
          needs_code: false,
          user: { id: "u1", email: "a@example.test", display_name: "A", role: "member", disabled_at: null },
          reenrolment_grant: "grant-1",
        };
      if (path === "/me/authenticator")
        return { token: "offer-1", secret: SECRET, uri: STARTED.otpauth_uri };
      return { devices_revoked: 0, other_sessions_ended: 0 };
    });
    const { container } = render(<SignIn onDone={() => {}} />);
    type("Email", "a@example.test");
    type("Password", PASSWORD);
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    fireEvent.click(await screen.findByRole("button", { name: /use a recovery code/ }));
    type("Recovery code", "aaaaa11111");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await screen.findByText("A new authenticator");
    expect(screen.getByLabelText("The six digits").getAttribute("autocomplete")).toBe(
      "one-time-code",
    );
    const offered = everythingHeld(container);
    expect(offered).toContain(SECRET);
    expect(offered).not.toContain(PASSWORD);

    type("The six digits", "135790");
    fireEvent.click(screen.getByRole("button", { name: "Check the code and finish" }));
    await vi.waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/me/authenticator/confirm", expect.anything()),
    );
    await vi.waitFor(() => expect(everythingHeld(container)).not.toContain(SECRET));
    expect(everythingHeld(container)).not.toContain("135790");
  });
});
