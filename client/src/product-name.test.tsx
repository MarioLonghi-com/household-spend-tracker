// @vitest-environment jsdom

/**
 * What the app is called (#346): "Household Spend Tracker" in the desktop app,
 * the name both stores list it under, and "Spend Tracker" on a server. The
 * server says which beside the capabilities; the screens read it, never the
 * profile.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

vi.mock("./lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "./lib/api";
import {
  EVERYTHING,
  loadCapabilities,
  productNameNow,
  setCapabilities,
  setProductName,
  useProductName,
} from "./lib/capabilities";
import { tabTitle } from "./App";
import { SignIn } from "./screens/SignIn";

afterEach(() => {
  cleanup();
  setCapabilities(null);
  setProductName(null);
  vi.mocked(api.get).mockReset();
});

function Shown() {
  return <p>{useProductName()}</p>;
}

describe("the product name", () => {
  it("is the server's until the boot says otherwise, and the desktop's once it does", async () => {
    render(<Shown />);
    expect(screen.getByText("Spend Tracker")).toBeTruthy();

    vi.mocked(api.get).mockResolvedValue({ ...EVERYTHING, snap: false, product_name: "Household Spend Tracker" });
    const offered = await loadCapabilities();

    await screen.findByText("Household Spend Tracker");
    expect(productNameNow()).toBe("Household Spend Tracker");
    // It travels beside the switches, not among them.
    expect(offered).toEqual({ ...EVERYTHING, snap: false });
    expect("product_name" in offered).toBe(false);
  });

  it("stays the server's for a server too old to say, or one that says nothing usable", async () => {
    for (const answer of [{ ...EVERYTHING }, { ...EVERYTHING, product_name: "" }, { ...EVERYTHING, product_name: 7 }]) {
      setProductName("Household Spend Tracker");
      vi.mocked(api.get).mockResolvedValue(answer);
      await loadCapabilities();
      expect(productNameNow()).toBe("Spend Tracker");
    }
  });

  it("starts the tab title on both", () => {
    expect(tabTitle("Accounts", "Casa Doe")).toBe("Spend Tracker - Accounts - Casa Doe");
    setProductName("Household Spend Tracker");
    expect(tabTitle("Accounts", "Flat 2")).toBe("Household Spend Tracker - Accounts - Flat 2");
    expect(tabTitle(null)).toBe("Household Spend Tracker");
  });

  it("heads the sign-in page", () => {
    vi.mocked(api.get).mockResolvedValue({ available: false, reason: "not_configured" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    setProductName("Household Spend Tracker");
    render(
      <QueryClientProvider client={client}>
        <SignIn onDone={() => {}} />
      </QueryClientProvider>,
    );
    expect(screen.getByRole("heading", { level: 1, name: "Household Spend Tracker" })).toBeTruthy();
  });
});
