// @vitest-environment jsdom

/**
 * The admin acts that were one stray click away (#199): a role change on a
 * dropdown's change event, Disable (which signs somebody out everywhere) and
 * Revoke. Each now asks first. Asserted by what reached the API: nothing
 * before the confirmation, exactly one request with the right body after it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

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
import { Admin } from "./Admin";
import type { AdminUser, Invitation, User } from "../lib/types";

const ME = { id: "user-a", email: "a@example.test", display_name: "Alice", role: "owner" } as User;

function person(id: string, name: string, over: Partial<AdminUser> = {}): AdminUser {
  return {
    id,
    email: `${id}@example.test`,
    display_name: name,
    role: "member",
    created_at: "2026-01-01T00:00:00Z",
    disabled_at: null,
    recovery_codes_left: 10,
    households: [],
    ...over,
  };
}

const USERS = [
  person("user-a", "Alice", { role: "owner" }),
  person("user-b", "Bea"),
  person("user-c", "Cal"),
  person("user-d", "Dee", { role: "owner" }),
];

const INVITES: Invitation[] = [
  {
    id: "inv-1",
    email: "dee@example.test",
    role: "member",
    invited_by_id: "user-a",
    household_ids: [],
    created_at: "2026-09-01T00:00:00Z",
    expires_at: "2026-10-08T00:00:00Z",
    accepted_at: null,
  },
  {
    id: "inv-2",
    email: "eve@example.test",
    role: "owner",
    invited_by_id: "user-a",
    household_ids: [],
    created_at: "2026-09-02T00:00:00Z",
    expires_at: "2026-10-09T00:00:00Z",
    accepted_at: null,
  },
];

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.del).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => {
    if (path === "/admin/users") return Promise.resolve(USERS);
    if (path === "/admin/invitations") return Promise.resolve(INVITES);
    return Promise.resolve([]);
  }) as typeof api.get);
  vi.mocked(api.post).mockResolvedValue({});
  vi.mocked(api.del).mockResolvedValue(null);
});

afterEach(cleanup);

async function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Admin user={ME} />
    </QueryClientProvider>,
  );
  await screen.findByText("Bea");
}

describe("a role change", () => {
  // Promotion to owner goes through the two-factor dialog (#205), which is its
  // own confirmation; demotion is the change this screen confirms (#199).
  it("asks before demoting, and sends that person's new role once", async () => {
    await mount();
    fireEvent.change(screen.getByRole("combobox", { name: "Role for Dee" }), {
      target: { value: "member" },
    });
    expect(api.post).not.toHaveBeenCalled();
    // The dropdown still says what the server says.
    expect((screen.getByRole("combobox", { name: "Role for Dee" }) as HTMLSelectElement).value).toBe(
      "owner",
    );

    const confirm = screen.getByRole("dialog", { name: "Make Dee a member?" });
    fireEvent.click(within(confirm).getByRole("button", { name: "Yes, make them a member" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/admin/users/user-d/role", { role: "member" });
  });

  it("sends nothing when left", async () => {
    await mount();
    fireEvent.change(screen.getByRole("combobox", { name: "Role for Dee" }), {
      target: { value: "member" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Leave it" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.post).not.toHaveBeenCalled();
  });

  it("sends nothing when a promotion is started, until both factors are given", async () => {
    await mount();
    fireEvent.change(screen.getByRole("combobox", { name: "Role for Cal" }), {
      target: { value: "owner" },
    });
    expect(screen.getByRole("dialog", { name: "Make Cal an owner?" })).toBeTruthy();
    expect(api.post).not.toHaveBeenCalled();
  });
});

describe("Disable", () => {
  it("asks first, and disables only the person it named", async () => {
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Disable Bea" }));
    expect(api.post).not.toHaveBeenCalled();

    const confirm = screen.getByRole("dialog", { name: "Disable Bea?" });
    expect(confirm.textContent).toContain("user-b@example.test");
    fireEvent.click(within(confirm).getByRole("button", { name: "Yes, disable them" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/admin/users/user-b/disabled", { disabled: true });
  });

  it("re-enables without asking", async () => {
    vi.mocked(api.get).mockImplementation(((path: string) =>
      Promise.resolve(
        path === "/admin/users"
          ? [USERS[0], { ...USERS[1], disabled_at: "2026-09-01T00:00:00Z" }, USERS[2]]
          : [],
      )) as typeof api.get);
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Re-enable Bea" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/admin/users/user-b/disabled", { disabled: false });
  });
});

describe("Revoke", () => {
  it("asks first, and revokes only the invitation it named", async () => {
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Invitations" }));
    await screen.findByText("eve@example.test");

    const row = screen.getByText("eve@example.test").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Revoke" }));
    expect(api.del).not.toHaveBeenCalled();

    const confirm = screen.getByRole("dialog", { name: "Revoke this invitation?" });
    expect(confirm.textContent).toContain("eve@example.test");
    fireEvent.click(within(confirm).getByRole("button", { name: "Keep it" }));
    expect(api.del).not.toHaveBeenCalled();

    fireEvent.click(within(row).getByRole("button", { name: "Revoke" }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, revoke it" }));
    await waitFor(() => expect(api.del).toHaveBeenCalledTimes(1));
    expect(api.del).toHaveBeenCalledWith("/admin/invitations/inv-2");
  });
});
