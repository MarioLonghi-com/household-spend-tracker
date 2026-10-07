// @vitest-environment jsdom

/**
 * Sign-in methods and the passkey list (#122, from #47 §3).
 *
 * The server here is a small fake with state: adding, renaming and removing
 * change what the next `GET /me/passkeys` returns, so each test asserts what
 * the act changed on screen *and* what was sent -- not that a request was
 * made. Two passkeys in the opening state, one of them made for another host.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { noteRecoveryReminder, rememberThisDevice } from "../lib/passkeys";
import type { Passkey, User } from "../lib/types";
import { Profile } from "./Profile";

const JANE: User = {
  id: "u1",
  email: "jane@example.com",
  display_name: "Jane",
  role: "owner",
  disabled_at: null,
};

const PHONE: Passkey = {
  id: "pk-phone",
  label: "iCloud Keychain",
  created_at: "2026-09-01T10:00:00",
  last_used_at: "2026-10-01T08:00:00",
  synced: true,
  rp_id: "spend.example.ts.net",
  usable_here: true,
  aaguid: "fbfc3007-154e-4ecc-8c0b-6e020557d7bd",
};
const OLD: Passkey = {
  id: "pk-old",
  label: "Work laptop",
  created_at: "2026-08-01T10:00:00",
  last_used_at: null,
  synced: false,
  rp_id: "old.example.ts.net",
  usable_here: false,
  aaguid: null,
};
const ADDED: Passkey = {
  id: "pk-new",
  label: "Google Password Manager",
  created_at: "2026-10-07T12:00:00",
  last_used_at: null,
  synced: true,
  rp_id: "spend.example.ts.net",
  usable_here: true,
  aaguid: "ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4",
};
const ADDRESS = "https://spend.example.ts.net";

let held: Passkey[];

function server(available: boolean | "browser" = true) {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path === "/me/passkeys") return held.map((one) => ({ ...one }));
    if (path === "/session/passkey/state")
      return available === false
        ? { available: false, reason: "wrong_host", address: ADDRESS }
        : { available: true, reason: null, address: ADDRESS };
    if (path === "/me/authenticator") return { enrolled: true, locked_by_key: false };
    if (path === "/me/recovery-codes") return { unused: 7 };
    if (path === "/me/keys") return [];
    throw new Error(`unexpected GET ${path}`);
  });
  vi.mocked(api.post).mockImplementation(async (path: string) => {
    if (path === "/me/step-up") return { token: "grant-1", expires_at: "2026-10-07T12:05:00" };
    if (path === "/me/passkeys/options") return { challenge: "Y2g", rp: { id: "spend.example.ts.net" } };
    if (path === "/me/passkeys") {
      held = [ADDED, ...held];
      return ADDED;
    }
    throw new Error(`unexpected POST ${path}`);
  });
  vi.mocked(api.patch).mockImplementation(async (path: string, body: unknown) => {
    const id = path.split("/").pop();
    held = held.map((one) => (one.id === id ? { ...one, label: (body as { label: string }).label } : one));
    return held.find((one) => one.id === id);
  });
  vi.mocked(api.del).mockImplementation(async (path: string) => {
    const id = path.split("/").pop();
    held = held.filter((one) => one.id !== id);
    return undefined;
  });
}

function aBrowserWithPasskeys(supported = true) {
  const create = vi.fn(async () => ({ toJSON: () => ({ id: "cred-new", type: "public-key" }) }));
  if (supported) {
    vi.stubGlobal("PublicKeyCredential", {
      parseCreationOptionsFromJSON: (json: unknown) => json,
      parseRequestOptionsFromJSON: (json: unknown) => json,
    });
    Object.defineProperty(window, "isSecureContext", { value: true, configurable: true });
    Object.defineProperty(navigator, "credentials", { value: { create }, configurable: true });
  }
  return create;
}

function profile() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Profile user={JANE} households={[]} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

const rowOf = (label: string) => screen.getByText(label).closest("tr") as HTMLElement;
const names = () =>
  Array.from(document.querySelectorAll("table.passkeys tbody tr strong")).map((one) => one.textContent);

beforeEach(() => {
  held = [PHONE, OLD];
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  for (const fn of [api.get, api.post, api.patch, api.del]) vi.mocked(fn).mockReset();
  Object.defineProperty(window, "isSecureContext", { value: false, configurable: true });
  Object.defineProperty(navigator, "credentials", { value: undefined, configurable: true });
});

describe("Sign-in methods", () => {
  it("says what gets you in, and each method's state in words", async () => {
    aBrowserWithPasskeys();
    server();
    rememberThisDevice(PHONE.id);
    profile();

    expect(
      await screen.findByText("You sign in with a passkey, or with your password and authenticator code."),
    ).toBeTruthy();
    expect(await screen.findByText("1 passkey")).toBeTruthy();
    expect(screen.getByText("7 of 10 left")).toBeTruthy();
    expect(screen.getByText("Set")).toBeTruthy();
    expect(screen.getByText("Set up")).toBeTruthy();
    // Keys for programs is its own section, not a sign-in method.
    expect(screen.getByRole("heading", { name: "Keys for programs" })).toBeTruthy();

    // "This device" on the passkey this browser signed in with, and only there.
    expect(within(rowOf("iCloud Keychain")).getByText("this device")).toBeTruthy();
    expect(within(rowOf("Work laptop")).queryByText("this device")).toBeNull();
    expect(within(rowOf("iCloud Keychain")).getByText("Synced")).toBeTruthy();
    expect(within(rowOf("Work laptop")).getByText("This device only")).toBeTruthy();
  });

  it("marks a passkey made for another host, and offers only to remove it", async () => {
    aBrowserWithPasskeys();
    server();
    profile();
    const old = rowOf(await screen.findByText("Work laptop").then((one) => one.textContent!));
    expect(within(old).getByText("Made for old.example.ts.net, cannot be used here.")).toBeTruthy();
    expect(within(old).queryByRole("button", { name: /Rename/ })).toBeNull();
    expect(within(old).getByRole("button", { name: "Remove Work laptop" })).toBeTruthy();
  });

  it("with no passkeys says so, and that only the password and code get you in", async () => {
    aBrowserWithPasskeys();
    server();
    held = [];
    profile();
    expect(await screen.findByText("You sign in with your password and authenticator code.")).toBeTruthy();
    expect(await screen.findByText("None yet")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Add a passkey" })).toBeTruthy();
  });
});

describe("where passkeys cannot work", () => {
  it("there is no Add button, only the reason, with the address that works", async () => {
    aBrowserWithPasskeys();
    server(false);
    profile();
    expect(
      await screen.findByText(`Passkeys need this app opened at its HTTPS address, ${ADDRESS}.`),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add a passkey" })).toBeNull();
    // The list still shows, so a passkey can still be removed from here.
    expect(screen.getByText("iCloud Keychain")).toBeTruthy();
  });

  it("a browser without passkey support is told so", async () => {
    aBrowserWithPasskeys(false);
    server();
    held = [];
    profile();
    expect(await screen.findByText("This browser does not support passkeys yet.")).toBeTruthy();
    expect(screen.getByText("Not available here")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add a passkey" })).toBeNull();
  });
});

describe("the passkey list", () => {
  it("adds one with the step-up asked inline, then the browser's prompt, and opens its name", async () => {
    const create = aBrowserWithPasskeys();
    server();
    profile();
    fireEvent.click(await screen.findByRole("button", { name: "Add a passkey" }));
    fireEvent.change(screen.getByLabelText("Your password"), { target: { value: "a long password" } });
    fireEvent.change(screen.getByLabelText("The six digits from your authenticator"), {
      target: { value: "123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Continue to the passkey" }));

    const name = (await screen.findByLabelText(`New name for ${ADDED.label}`)) as HTMLInputElement;
    expect(name.value).toBe(ADDED.label);
    expect(document.activeElement).toBe(name);
    expect(name.closest("tr")!.className).toBe("fresh");
    expect(api.post).toHaveBeenCalledWith("/me/step-up", { password: "a long password", code: "123456" });
    expect(api.post).toHaveBeenCalledWith("/me/passkeys/options", { step_up_token: "grant-1" });
    expect(create).toHaveBeenCalledTimes(1);
    expect(api.post).toHaveBeenCalledWith("/me/passkeys", {
      credential: { id: "cred-new", type: "public-key" },
    });
    expect(await screen.findByText("2 passkeys")).toBeTruthy();
    // The password field is not left holding anything.
    expect(screen.queryByLabelText("Your password")).toBeNull();
  });

  it("renames in place", async () => {
    aBrowserWithPasskeys();
    server();
    profile();
    fireEvent.click(await screen.findByRole("button", { name: "Rename iCloud Keychain" }));
    fireEvent.change(screen.getByLabelText("New name for iCloud Keychain"), {
      target: { value: "Phone" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Phone")).toBeTruthy();
    expect(api.patch).toHaveBeenCalledWith("/me/passkeys/pk-phone", { label: "Phone" });
    expect(screen.queryByText("iCloud Keychain")).toBeNull();
  });

  it("asks once before removing, says what is left, and removes only that one", async () => {
    aBrowserWithPasskeys();
    server();
    profile();
    fireEvent.click(await screen.findByRole("button", { name: "Remove iCloud Keychain" }));
    const question = screen.getByRole("alertdialog");
    expect(question.textContent).toContain("You will still be able to sign in with your password and code.");
    expect(api.del).not.toHaveBeenCalled();

    fireEvent.click(within(question).getByRole("button", { name: "Remove" }));
    await vi.waitFor(() => expect(screen.queryByText("iCloud Keychain")).toBeNull());
    expect(api.del).toHaveBeenCalledWith("/me/passkeys/pk-phone");
    expect(screen.getByText("Work laptop")).toBeTruthy();
    expect(await screen.findByText("You sign in with your password and authenticator code.")).toBeTruthy();
  });

  it("keeping it changes nothing", async () => {
    aBrowserWithPasskeys();
    server();
    profile();
    fireEvent.click(await screen.findByRole("button", { name: "Remove iCloud Keychain" }));
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.del).not.toHaveBeenCalled();
    expect(screen.getByText("iCloud Keychain")).toBeTruthy();
  });

  it("sorts at its column headers", async () => {
    aBrowserWithPasskeys();
    server();
    held = [OLD, PHONE];
    profile();
    await screen.findByText("Work laptop");
    expect(names()).toEqual(["Work laptop", "iCloud Keychain"]);
    fireEvent.click(screen.getByRole("button", { name: /^Name/ }));
    expect(names()).toEqual(["iCloud Keychain", "Work laptop"]);
    fireEvent.click(screen.getByRole("button", { name: /^Name/ }));
    expect(names()).toEqual(["Work laptop", "iCloud Keychain"]);
    // Never used sorts after every date.
    fireEvent.click(screen.getByRole("button", { name: /^Last used/ }));
    expect(names()).toEqual(["iCloud Keychain", "Work laptop"]);
  });

  it("with one passkey, suggests a second on another device", async () => {
    aBrowserWithPasskeys();
    server();
    held = [PHONE];
    profile();
    expect(await screen.findByText(/Add a second on another device/)).toBeTruthy();
  });

  it("after a recovery-code sign-in, repeats the reminder until it is dismissed", async () => {
    aBrowserWithPasskeys();
    server();
    noteRecoveryReminder(1);
    profile();
    const reminder = await screen.findByText(/You signed in with a recovery code/);
    fireEvent.click(within(reminder).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(/You signed in with a recovery code/)).toBeNull();
    expect(window.sessionStorage.length).toBe(0);
  });

  it("every control is a labelled button or field, reachable from the keyboard", async () => {
    aBrowserWithPasskeys();
    server();
    profile();
    await screen.findByText("iCloud Keychain");
    for (const button of within(document.querySelector("table.passkeys")!).getAllByRole("button")) {
      expect(button.getAttribute("tabindex")).not.toBe("-1");
      expect((button.getAttribute("aria-label") ?? button.textContent ?? "").trim()).not.toBe("");
    }
  });
});
