// @vitest-environment jsdom

/**
 * Opening the desktop app with Touch ID, on the profile screen (#432).
 *
 * - The row is there only where the server offers `device_unlock` *and* the
 *   shell says this computer can prompt: a server, or a desktop without a
 *   sensor, shows nothing at all.
 * - Turning it on sends the password and the method, and the row then reads
 *   On; nothing is sent while the password field is empty.
 * - Turning it off sends a DELETE with no password, and the row reads Off.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

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
import { EVERYTHING, setCapabilities } from "../lib/capabilities";
import { Profile } from "./Profile";
import type { User } from "../lib/types";

type Holder = { spendTrackerDesktop?: unknown };

const JANE = { id: "u1", email: "jane@example.test", display_name: "Jane", role: "owner" } as unknown as User;
const DESKTOP = { ...EVERYTHING, agents: false, second_factor: false, password_reset_by_code: true, device_unlock: true };

let enabled = false;

afterEach(() => {
  cleanup();
  setCapabilities(null);
  delete (globalThis as Holder).spendTrackerDesktop;
});

beforeEach(() => {
  enabled = false;
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.del).mockReset();
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === "/me/device-unlock") return Promise.resolve({ enabled, method: enabled ? "touch_id" : null, created_at: null });
    if (path === "/me/recovery-codes") return Promise.resolve({ unused: 1 });
    if (path === "/me/passkeys") return Promise.resolve([]);
    if (path === "/session/passkey/state") return Promise.resolve({ available: false });
    return Promise.reject(new Error(`unexpected ${path}`));
  });
  vi.mocked(api.post).mockImplementation(() => {
    enabled = true;
    return Promise.resolve({ enabled: true, method: "touch_id", created_at: "2026-11-12T08:00:00Z" });
  });
  vi.mocked(api.del).mockImplementation(() => {
    enabled = false;
    return Promise.resolve(null);
  });
});

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Profile user={JANE} households={[]} onClose={() => undefined} />
    </QueryClientProvider>,
  );
}

describe("Open with Touch ID on the profile screen (#432)", () => {
  it("is not there on a server, nor on a desktop without a sensor", async () => {
    setCapabilities({ ...EVERYTHING, device_unlock: false });
    (globalThis as Holder).spendTrackerDesktop = { setLanguage: () => undefined, unlock: "touch_id" };
    mount();
    await screen.findByText("Password");
    expect(screen.queryByText("Open with Touch ID")).toBeNull();
    cleanup();

    setCapabilities(DESKTOP);
    (globalThis as Holder).spendTrackerDesktop = { setLanguage: () => undefined };
    mount();
    await screen.findByText("Password");
    expect(screen.queryByText("Open with Touch ID")).toBeNull();
    expect(vi.mocked(api.get).mock.calls.map(([path]) => path)).not.toContain("/me/device-unlock");
  });

  it("turns on with the password and the method, then reads On", async () => {
    setCapabilities(DESKTOP);
    (globalThis as Holder).spendTrackerDesktop = { setLanguage: () => undefined, unlock: "touch_id" };
    mount();
    await screen.findByText("Open with Touch ID");
    expect(await screen.findByText("Off")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Turn on: Open with Touch ID" }));

    const submit = () => screen.getByRole("button", { name: "Turn on Touch ID" });
    expect((submit() as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Your password"), { target: { value: "a sufficiently long password" } });
    fireEvent.click(submit());

    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/me/device-unlock", { password: "a sufficiently long password", method: "touch_id" }));
    expect(await screen.findByText("On")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Turn off: Open with Touch ID" })).toBeTruthy();
  });

  it("turns off with no password, then reads Off", async () => {
    enabled = true;
    setCapabilities(DESKTOP);
    (globalThis as Holder).spendTrackerDesktop = { setLanguage: () => undefined, unlock: "touch_id" };
    mount();
    await screen.findByText("Open with Touch ID");
    expect(await screen.findByText("On")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Turn off: Open with Touch ID" }));
    fireEvent.click(screen.getByRole("button", { name: "Turn off Touch ID" }));
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/me/device-unlock"));
    expect(api.post).not.toHaveBeenCalled();
    expect(await screen.findByText("Off")).toBeTruthy();
  });
});
