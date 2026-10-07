// @vitest-environment jsdom

/**
 * #54's exit: none of the screens a person sees first shows unaccented text
 * in the en-XA pseudo-locale.
 *
 * Each screen is rendered in en-XA and every word it shows -- text, and the
 * labels a screen reader or a tooltip reads -- is checked. A word made only of
 * plain ASCII letters is one nobody extracted. Data is exempt (a name, an
 * address, a code to type), and so is the product's name, which stays as it
 * is in every language.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
  setUnauthorizedHandler: vi.fn(),
}));
vi.mock("../lib/passkeys", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/passkeys")>()),
  passkeyState: vi.fn(async () => ({ available: true, reason: null, address: null })),
  conditionalMediationAvailable: vi.fn(async () => false),
}));

import { api } from "../lib/api";
import { activate } from "../lib/i18n";
import { RecoveryCodeSheet } from "../components/RecoveryCodes";
import { RecoveryModeBanner } from "../components/RecoveryModeBanner";
import { ReenrolmentDue } from "../components/ReenrolmentDue";
import { SignInNotice } from "../components/SignInNotice";
import { NO_PROOF, StepUpFields } from "../components/StepUp";
import type { User } from "../lib/types";
import { AcceptInvite } from "./AcceptInvite";
import { Profile } from "./Profile";
import { ResetAccount } from "./ResetAccount";
import { Setup } from "./Setup";
import { SignIn } from "./SignIn";

const ROBIN: User = {
  id: "u1",
  email: "robin@example.com",
  display_name: "Robin",
  role: "owner",
  disabled_at: null,
};

/** Shown as typed or as sent, in every language. */
const DATA = new Set([
  "Spend",
  "Tracker",
  "Robin",
  "Sam",
  "robin@example.com",
  "Casa",
  "Doe",
  "Laptop",
  "SPENDTRACKER_SECRET_KEY",
  "secret.key",
  "ABCDEFGH",
  "Claude",
  "Desktop",
  "HTTPS",
  // Lists are joined by `listText` (Intl's own conjunction outside English),
  // not by the catalog, so the pseudo-locale cannot accent it.
  "and",
]);

/** Words of plain ASCII letters, from the text and the labels people are given. */
function untranslated(root: HTMLElement): string[] {
  const shown: string[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    // Code to type, not words to read.
    if ((node.parentElement?.closest(".mono, .codes, svg") ?? null) !== null) continue;
    shown.push(node.textContent ?? "");
  }
  for (const element of Array.from(root.querySelectorAll("[aria-label], [title], [placeholder]"))) {
    for (const name of ["aria-label", "title", "placeholder"]) {
      const value = element.getAttribute(name);
      if (value) shown.push(value);
    }
  }
  return shown
    .map((text) => text.replace(/\S+@\S+/g, " "))
    .flatMap((text) => text.split(/[\s.,;:!?()"'…·—–/-]+/))
    .filter((word) => /^[A-Za-z]{3,}$/.test(word) && !DATA.has(word));
}

function withQueries(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(async () => {
  await activate("en-XA");
});

afterEach(async () => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
  await activate("en");
});

describe("in en-XA, the first screens show no English", () => {
  it("signing in, both steps", async () => {
    vi.mocked(api.post).mockResolvedValue({ authenticated: false, needs_code: true, user: null });
    const { container } = render(<SignIn onDone={vi.fn()} />);
    expect(untranslated(container)).toEqual([]);

    const fields = container.querySelectorAll("input");
    fireEvent.change(fields[0], { target: { value: ROBIN.email } });
    fireEvent.change(fields[1], { target: { value: "a long password" } });
    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() => expect(container.querySelector('input[name="one-time-code"]')).not.toBeNull());
    expect(untranslated(container)).toEqual([]);

    // And the recovery-code half of the same step.
    fireEvent.click(container.querySelector("button.link")!);
    await waitFor(() => expect(container.querySelector('input[name="recovery-code"]')).not.toBeNull());
    expect(untranslated(container)).toEqual([]);
  });

  it("setting up the instance", async () => {
    vi.mocked(api.get).mockResolvedValue({ setup_required: true, token_path: null });
    const { container } = render(withQueries(<Setup onDone={vi.fn()} />));
    await waitFor(() => expect(container.querySelector('input[name="setup-token"]')).not.toBeNull());
    expect(untranslated(container)).toEqual([]);
  });

  it("accepting an invitation, as an owner and into two households", async () => {
    vi.mocked(api.get).mockResolvedValue({
      role: "owner",
      email: ROBIN.email,
      invited_by: "Sam",
      households: ["Casa", "Doe"],
    });
    const { container } = render(<AcceptInvite token="t" onDone={vi.fn()} />);
    await waitFor(() => expect(container.querySelector('input[name="email"]')).not.toBeNull());
    expect(untranslated(container)).toEqual([]);
  });

  it("an invitation that does not work", async () => {
    vi.mocked(api.get).mockRejectedValue(new Error("gone"));
    const { container } = render(<AcceptInvite token="t" onDone={vi.fn()} />);
    await waitFor(() => expect(container.querySelector("h1")).not.toBeNull());
    expect(untranslated(container).filter((word) => word !== "gone")).toEqual([]);
  });

  it("resetting a sign-in, password and authenticator", async () => {
    vi.mocked(api.get).mockResolvedValue({
      email: ROBIN.email,
      display_name: "Robin",
      password: true,
      authenticator: true,
      reset_by: "Sam",
      created_at: "2026-03-02T10:15:00",
    });
    vi.mocked(api.post).mockResolvedValue({ token: "x", secret: "ABCDEFGH", otpauth_uri: "otpauth://x" });
    const { container } = render(<ResetAccount token="t" onDone={vi.fn()} />);
    await waitFor(() => expect(container.querySelector('input[name="new-password"]')).not.toBeNull());
    // formatInstant writes the date the browser's way; its month is a word.
    const left = untranslated(container).filter((word) => !/^(AM|PM)$/.test(word));
    expect(left).toEqual([]);
  });

  it("the profile, with its sign-in methods and keys", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/me/authenticator") return { enrolled: true, locked_by_key: false };
      if (path === "/me/recovery-codes") return { unused: 3 };
      if (path === "/me/passkeys")
        return [
          {
            id: "p1",
            label: "Laptop",
            synced: true,
            usable_here: true,
            rp_id: "example.test",
            created_at: "2026-03-02T10:15:00",
            last_used_at: null,
          },
        ];
      if (path === "/me/keys") return [];
      throw new Error(`unexpected ${path}`);
    });
    render(withQueries(<Profile user={ROBIN} households={[]} onClose={vi.fn()} />));
    await screen.findByText("Laptop");
    // The panel is portalled; check the whole document.
    const left = untranslated(document.body).filter((word) => !/^(AM|PM)$/.test(word));
    expect(left).toEqual([]);
  });

  it("the pieces the shell shows above a screen", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/admin/recovery-mode") return { locked: 2, key_from_environment: false };
      if (path === "/me/authenticator") return { enrolled: true, locked_by_key: true };
      if (path === "/admin/sign-in-changes")
        return [
          {
            id: 9,
            key: "k9",
            what: "reset",
            password: true,
            authenticator: false,
            user_id: "u2",
            user_name: "Sam",
            by_id: "u3",
            by_name: null,
            from_server: true,
            at: "2026-03-02T10:15:00",
          },
        ];
      throw new Error(`unexpected ${path}`);
    });
    const { container } = render(
      withQueries(
        <>
          <RecoveryModeBanner />
          <ReenrolmentDue user={ROBIN} onOpen={vi.fn()} />
          <SignInNotice user={ROBIN} onOpen={vi.fn()} />
          <StepUpFields proof={NO_PROOF} onChange={vi.fn()} why="" />
          <RecoveryCodeSheet codes={["0a1b2c3d4e"]} action="" onStored={vi.fn()} />
        </>,
      ),
    );
    await waitFor(() => expect(container.querySelectorAll('[role="status"]').length).toBe(3));
    expect(untranslated(container)).toEqual([]);
  });
});
