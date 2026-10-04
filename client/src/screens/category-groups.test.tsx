// @vitest-environment jsdom

/**
 * Renaming and deleting a category group from its heading (#184).
 *
 * What is pinned: the panel sends the new name to *that* group; Delete sends
 * the delete for that group; and a refused delete shows the server's own
 * words in the alert rather than a rule restated here -- the screen cannot
 * see archived categories, so it is not the one that can decide.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import type { Category, CategoryGroup } from "../lib/types";
import { CategorySettings, GroupSettings } from "./Categories";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const BILLS: CategoryGroup = { id: "g-bills", name: "Bills", sort_order: 1, categories: [] };

function mount(group: CategoryGroup = BILLS) {
  const onSaved = vi.fn();
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <GroupSettings group={group} onClose={vi.fn()} onSaved={onSaved} />
    </QueryClientProvider>,
  );
  return onSaved;
}

describe("a group's panel", () => {
  it("sends the new label to that group", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...BILLS, name: "Fixed costs" });
    const onSaved = mount();

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toHaveProperty("disabled", true); // nothing changed yet
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Fixed costs" } });
    fireEvent.click(save);

    await vi.waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(api.patch).toHaveBeenCalledWith("/category-groups/g-bills", { name: "Fixed costs" });
  });

  it("deletes that group", async () => {
    vi.mocked(api.del).mockResolvedValue(undefined);
    const onSaved = mount();

    fireEvent.click(screen.getByRole("button", { name: "Delete group" }));
    // Asked first (#199): nothing is sent until the confirmation is.
    expect(api.del).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Yes, delete it" }));

    await vi.waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(api.del).toHaveBeenCalledTimes(1);
    expect(api.del).toHaveBeenCalledWith("/category-groups/g-bills");
  });

  it("keeps the group when the confirmation is declined", () => {
    const onSaved = mount();

    fireEvent.click(screen.getByRole("button", { name: "Delete group" }));
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));

    expect(screen.queryByRole("button", { name: "Yes, delete it" })).toBeNull();
    expect(api.del).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("says in the alert why a group with categories under it stays", async () => {
    const said =
      "A group can only be deleted when there are no categories under it. " +
      "'Bills' still holds 2 categories, 1 of them archived. Move or delete them first.";
    vi.mocked(api.del).mockRejectedValue(new Error(said));
    const onSaved = mount();

    fireEvent.click(screen.getByRole("button", { name: "Delete group" }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, delete it" }));

    expect((await screen.findByRole("alert")).textContent).toBe(said);
    expect(onSaved).not.toHaveBeenCalled();
  });
});

describe("a category's panel (#199)", () => {
  const RENT: Category = {
    id: "c-rent",
    group_id: "g-bills",
    name: "Rent",
    full_name: "Bills: Rent",
    sort_order: 1,
    archived: false,
    used_by: 0,
  };

  function mountCategory() {
    const onSaved = vi.fn();
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <CategorySettings
          category={RENT}
          stat={undefined}
          groups={[BILLS]}
          onClose={vi.fn()}
          onSaved={onSaved}
        />
      </QueryClientProvider>,
    );
    return onSaved;
  }

  it("asks before deleting, and deletes that category once on Yes", async () => {
    vi.mocked(api.del).mockResolvedValue(undefined);
    const onSaved = mountCategory();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(api.del).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Delete the category Rent?" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Yes, delete it" }));
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(api.del).toHaveBeenCalledTimes(1);
    expect(api.del).toHaveBeenCalledWith("/categories/c-rent");
  });

  it("sends nothing when kept", () => {
    const onSaved = mountCategory();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));
    expect(api.del).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });
});
