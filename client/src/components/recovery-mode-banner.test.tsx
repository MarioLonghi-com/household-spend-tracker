// @vitest-environment jsdom

/**
 * The owners' banner in recovery mode (#287): how many members' authenticators
 * this server's key cannot open, and nothing at all when it opens every one.
 * It names the key where it came from: `secret.key`, or the environment
 * variable that wins over it. And it keeps asking while it stays on screen,
 * so it follows the server without a reload.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { RECOVERY_MODE_POLL_MS, RecoveryModeBanner } from "./RecoveryModeBanner";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

/** What the server says, from now on. */
function answer(locked: number) {
  vi.mocked(api.get).mockResolvedValue({ enrolled: 2, locked, key_from_environment: false });
}

/** Mounted once, as the shell mounts it, with the app's own query defaults (main.tsx). */
function mountedInTheShell() {
  const client = new QueryClient({
    defaultOptions: { queries: { refetchOnWindowFocus: false, retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RecoveryModeBanner />
    </QueryClientProvider>,
  );
}

function show(locked: number, key_from_environment = false) {
  vi.mocked(api.get).mockResolvedValue({ enrolled: 2, locked, key_from_environment });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const shown = render(
    <QueryClientProvider client={client}>
      <RecoveryModeBanner />
    </QueryClientProvider>,
  );
  return { ...shown, client };
}

describe("the recovery-mode banner", () => {
  it("counts the members the key does not open", async () => {
    show(2);
    const banner = await screen.findByRole("status");
    expect(banner.textContent).toContain("does not open 2 members' authenticators");
    expect(banner.textContent).toContain("recovery code");
    expect(banner.textContent).toMatch(/^secret\.key does not open/);
    expect(banner.textContent).toContain(
      "Putting the original key back ends this for everyone who has not re-enrolled yet.",
    );
    expect(api.get).toHaveBeenCalledWith("/admin/recovery-mode");
  });

  it("names SPENDTRACKER_SECRET_KEY, not secret.key, when that is where the key came from", async () => {
    show(2, true);
    const banner = (await screen.findByRole("status")).textContent ?? "";
    expect(banner).toMatch(/^SPENDTRACKER_SECRET_KEY does not open 2 members' authenticators/);
    expect(banner).not.toContain("secret.key");
    expect(banner).toContain(
      "Setting it back to the original key ends this for everyone who has not re-enrolled yet.",
    );
  });

  it("says one member in the singular", async () => {
    show(1);
    expect((await screen.findByRole("status")).textContent).toContain("1 member's authenticator;");
  });

  it("is not there when the key opens everyone", async () => {
    const { container, client } = show(0);
    // Answered, not merely asked: an empty container before the answer
    // would say nothing.
    await vi.waitFor(() =>
      expect(client.getQueryState(["recovery-mode"])?.status).toBe("success"),
    );
    expect(container.textContent).toBe("");
  });

  it("goes without a reload once the server says nobody is locked out", async () => {
    // A member re-enrols in their own browser, or the operator puts the key
    // back: the owner's tab, open all along, has to notice by itself.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    answer(1);
    mountedInTheShell();
    expect((await screen.findByRole("status")).textContent).toContain("1 member's authenticator");

    answer(0);
    await vi.advanceTimersByTimeAsync(RECOVERY_MODE_POLL_MS);
    await vi.waitFor(() => expect(screen.queryByRole("status")).toBeNull());
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it("comes without a reload once the server says somebody is locked out", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    answer(0);
    mountedInTheShell();
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("status")).toBeNull();

    answer(2);
    await vi.advanceTimersByTimeAsync(RECOVERY_MODE_POLL_MS);
    expect((await screen.findByRole("status")).textContent).toContain("2 members' authenticators");
  });
});
