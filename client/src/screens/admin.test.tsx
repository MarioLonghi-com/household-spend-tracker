// @vitest-environment jsdom

/**
 * Making an owner costs both factors (#205).
 *
 * What is pinned: promoting a member to owner, and creating an owner-role
 * invitation, each buy a step-up grant with the password and a code *inside
 * the click that spends it*, and send it; demoting and member-role invitations
 * send none. Two people, so the promotion is aimed at the right one.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import type { AdminUser, User } from "../lib/types";
import { Admin } from "./Admin";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const ME: AdminUser = {
  id: "u-owner",
  email: "jane@example.com",
  display_name: "Jane",
  role: "owner",
  created_at: "2026-09-01T08:00:00Z",
  disabled_at: null,
  recovery_codes_left: 10,
  households: [],
};
const PARTNER: AdminUser = { ...ME, id: "u-partner", email: "sam@example.com", display_name: "Sam", role: "member" };
const OTHER_OWNER: AdminUser = { ...ME, id: "u-other", email: "alex@example.com", display_name: "Alex" };

function mount() {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path === "/admin/users") return [ME, PARTNER, OTHER_OWNER];
    return [];
  });
  vi.mocked(api.post).mockImplementation(async (path: string) => {
    if (path === "/me/step-up") return { token: "grant-once" };
    if (path === "/admin/invitations")
      return {
        invitation: {
          id: "i1",
          email: null,
          role: "owner",
          invited_by_id: ME.id,
          household_ids: [],
          created_at: "2026-10-01T08:00:00Z",
          expires_at: "2026-10-08T08:00:00Z",
          accepted_at: null,
        },
        link: "https://example.test/invite/abc",
      };
    return PARTNER;
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const me: User = { id: ME.id, email: ME.email, display_name: ME.display_name, role: "owner", disabled_at: null };
  render(
    <QueryClientProvider client={client}>
      <Admin user={me} />
    </QueryClientProvider>,
  );
}

function prove(scope: HTMLElement) {
  fireEvent.change(within(scope).getByLabelText("Your password"), {
    target: { value: "a sufficiently long password" },
  });
  fireEvent.change(within(scope).getByLabelText("The six digits from your authenticator"), {
    target: { value: "123456" },
  });
}

describe("promoting", () => {
  it("to owner asks for both factors first and spends one grant on that person", async () => {
    mount();
    const select = await screen.findByLabelText("Role for Sam");
    fireEvent.change(select, { target: { value: "owner" } });

    // Nothing sent yet: the dropdown only opened the question.
    expect(api.post).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", { name: "Make Sam an owner?" });
    const confirm = within(dialog).getByRole("button", { name: "Make them an owner" });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);

    prove(dialog);
    fireEvent.click(confirm);
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledTimes(2));

    expect(vi.mocked(api.post).mock.calls).toEqual([
      ["/me/step-up", { password: "a sufficiently long password", code: "123456" }],
      ["/admin/users/u-partner/role", { role: "owner", step_up_token: "grant-once" }],
    ]);
  });

  it("demoting asks once and then sends the new role, with no second factor", async () => {
    mount();
    const select = await screen.findByLabelText("Role for Alex");
    fireEvent.change(select, { target: { value: "member" } });
    expect(api.post).not.toHaveBeenCalled();

    const confirm = screen.getByRole("dialog", { name: "Make Alex a member?" });
    fireEvent.click(within(confirm).getByRole("button", { name: "Yes, make them a member" }));
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/admin/users/u-other/role", { role: "member" });
  });
});

describe("inviting", () => {
  async function openInvite() {
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Invitations" }));
    fireEvent.click(await screen.findByRole("button", { name: "Invite somebody" }));
    return screen.getByRole("dialog", { name: "Invite somebody" });
  }

  it("an owner link buys a grant with the password and a code and sends it", async () => {
    const panel = await openInvite();
    fireEvent.change(within(panel).getByLabelText("Role"), { target: { value: "owner" } });
    const create = within(panel).getByRole("button", { name: "Create the link" });
    expect((create as HTMLButtonElement).disabled).toBe(true);

    prove(panel);
    fireEvent.click(create);
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.post).mock.calls[0][0]).toBe("/me/step-up");
    expect(vi.mocked(api.post).mock.calls[1]).toEqual([
      "/admin/invitations",
      { role: "owner", email: null, household_ids: [], step_up_token: "grant-once" },
    ]);
  });

  it("a member link asks for nothing and sends no grant", async () => {
    const panel = await openInvite();
    expect(within(panel).queryByLabelText("Your password")).toBeNull();
    fireEvent.click(within(panel).getByRole("button", { name: "Create the link" }));
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/admin/invitations", {
      role: "member",
      email: null,
      household_ids: [],
      step_up_token: null,
    });
  });
});
