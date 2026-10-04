// @vitest-environment jsdom

/**
 * Making new recovery codes from the profile screen (#289).
 *
 * - The count of unused codes is shown, and refreshed after a new set.
 * - The form sends the password and the six digits, and nothing is sent
 *   until both are there -- a ten-character recovery code does not enable it.
 * - The new codes are shown once, behind the same "I have stored these" box
 *   as setup, and leave the page when it is ticked and dismissed.
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
import { RecoveryCodesSection } from "./Profile";

const NEW_CODES = ["0a1b2c3d4e", "5f6a7b8c9d"];

let unused = 3;

afterEach(cleanup);

beforeEach(() => {
  unused = 3;
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.get).mockImplementation((path: string) =>
    path === "/me/recovery-codes"
      ? Promise.resolve({ unused })
      : Promise.reject(new Error(`unexpected ${path}`)),
  );
  vi.mocked(api.post).mockImplementation(() => {
    unused = 10;
    return Promise.resolve({ codes: NEW_CODES });
  });
});

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <RecoveryCodesSection />
    </QueryClientProvider>,
  );
}

describe("recovery codes on the profile screen", () => {
  it("shows how many are left", async () => {
    mount();
    expect(await screen.findByText("3 unused recovery codes left.")).toBeTruthy();
  });

  it("warns when none are left", async () => {
    unused = 0;
    mount();
    expect(await screen.findByText(/You have no unused recovery codes/)).toBeTruthy();
  });

  it("sends both factors, shows the codes once, and refreshes the count", async () => {
    mount();
    await screen.findByText("3 unused recovery codes left.");
    fireEvent.click(screen.getByRole("button", { name: "Make new codes" }));

    const submit = () => screen.getAllByRole("button", { name: "Make new codes" })[0];
    fireEvent.change(screen.getByLabelText("Your password"), {
      target: { value: "my password" },
    });
    // A recovery code in the authenticator's place does not enable the button.
    fireEvent.change(screen.getByLabelText(/six digits from your authenticator/), {
      target: { value: "0a1b2c3d4e" },
    });
    expect((submit() as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText(/six digits from your authenticator/), {
      target: { value: "123 456" },
    });
    expect((submit() as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(submit());

    for (const one of NEW_CODES) expect(await screen.findByText(one)).toBeTruthy();
    expect(vi.mocked(api.post)).toHaveBeenCalledWith("/me/recovery-codes", {
      password: "my password",
      code: "123 456",
    });
    // The form is gone while the codes are on screen.
    expect(screen.queryByLabelText("Your password")).toBeNull();

    const done = screen.getByRole("button", { name: "Done" }) as HTMLButtonElement;
    expect(done.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText(/I have stored these/));
    expect(done.disabled).toBe(false);
    fireEvent.click(done);

    await waitFor(() => expect(screen.queryByText(NEW_CODES[0])).toBeNull());
    expect(await screen.findByText("10 unused recovery codes left.")).toBeTruthy();
  });
});
