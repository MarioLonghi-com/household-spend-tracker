// @vitest-environment jsdom

/**
 * The key form's category permission (#366): its own tick, offered only with
 * write, cleared with it, and sent as `may_edit_categories`. The assertion is
 * on the body that reaches the server -- the stored permission -- not on a
 * checkbox existing.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import type { Household } from "../lib/types";
import { IssueKey } from "./Profile";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const HOUSES = [
  { id: "h-home", name: "Home" },
  { id: "h-away", name: "Away" },
] as Household[];

function mount() {
  const onDone = vi.fn();
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <IssueKey households={HOUSES} onDone={onDone} onCancel={vi.fn()} />
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByLabelText("What is it for"), { target: { value: "tidier" } });
  fireEvent.change(screen.getByLabelText("Your password"), { target: { value: "pw" } });
  fireEvent.change(screen.getByLabelText("The six digits from your authenticator"), {
    target: { value: "123456" },
  });
  return onDone;
}

function issued() {
  const call = vi.mocked(api.post).mock.calls.find(([path]) => path === "/me/keys");
  return call?.[1] as Record<string, unknown> | undefined;
}

describe("the category permission on a new key", () => {
  it("is offered only with write, and sent when ticked", async () => {
    vi.mocked(api.post).mockImplementation(async (path: string) =>
      path === "/me/step-up" ? { token: "grant" } : { key: { label: "tidier" }, token: "st_x" },
    );
    const onDone = mount();
    const box = () => screen.queryByRole("checkbox", { name: /add categories/ });

    expect(box()).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: /add and change transactions/ }));
    fireEvent.click(box()!);
    fireEvent.click(screen.getByRole("button", { name: "Create the key" }));

    await vi.waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(issued()).toMatchObject({
      scope: "write",
      may_commit: false,
      may_edit_categories: true,
    });
  });

  it("is cleared when write is taken away again", async () => {
    vi.mocked(api.post).mockImplementation(async (path: string) =>
      path === "/me/step-up" ? { token: "grant" } : { key: { label: "tidier" }, token: "st_x" },
    );
    const onDone = mount();
    const write = screen.getByRole("checkbox", { name: /add and change transactions/ });

    fireEvent.click(write);
    fireEvent.click(screen.getByRole("checkbox", { name: /add categories/ }));
    fireEvent.click(write);
    fireEvent.click(write);
    expect(
      (screen.getByRole("checkbox", { name: /add categories/ }) as HTMLInputElement).checked,
    ).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Create the key" }));

    await vi.waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(issued()).toMatchObject({ scope: "write", may_edit_categories: false });
  });
});
