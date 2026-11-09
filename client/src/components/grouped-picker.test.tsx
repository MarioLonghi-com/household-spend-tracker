// @vitest-environment jsdom

/**
 * The picker's footer and its extra tick (#371).
 *
 * Select none used to clear the ids and leave the extra slice -- the
 * register's "Needs a category", the report's "Uncategorised" -- ticked, so
 * "none" still showed the slice. Checked by the state a caller ends up
 * holding, since that is what reaches the server; two groups of two, so a
 * group and an item cannot be mistaken for each other.
 */

import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { GroupedPicker } from "./GroupedPicker";
import type { PickerGroup, Selection } from "./GroupedPicker";

afterEach(cleanup);

const GROUPS: PickerGroup[] = [
  { key: "a", label: "Everyday", items: [{ id: "g", label: "Groceries" }, { id: "t", label: "Transport" }] },
  { key: "b", label: "Bills", items: [{ id: "r", label: "Rent" }, { id: "p", label: "Power" }] },
];

/** Holds what a caller would, the way the report does, and prints it. */
function Harness({ initialExtra }: { initialExtra: boolean }) {
  const [ids, setIds] = useState<Selection>(null);
  const [extra, setExtra] = useState(initialExtra);
  return (
    <>
      <GroupedPicker
        label="All categories"
        groups={GROUPS}
        value={ids}
        onChange={(next, tick) => {
          setIds(next);
          if (tick !== undefined) setExtra(tick);
        }}
        extra={{ label: "Uncategorised", checked: extra, onChange: setExtra }}
      />
      <output data-testid="state">{`${ids === null ? "all" : ids.join(",") || "none"}|${extra}`}</output>
    </>
  );
}

const open = () => {
  fireEvent.click(screen.getByRole("button", { name: /categories|Uncategorised|of/ }));
  return screen.getByRole("group", { name: "All categories" });
};
const state = () => screen.getByTestId("state").textContent;

describe("the picker's footer", () => {
  it("clears the extra tick on Select none", () => {
    render(<Harness initialExtra={true} />);
    const pop = open();
    fireEvent.click(within(pop).getByRole("button", { name: "Select none" }));
    expect(state()).toBe("none|false");
    expect(screen.getByRole("button", { name: /0 of 5/ })).toBeTruthy();
  });

  it("ticks the extra on Select all, so the label is the untouched one again", () => {
    render(<Harness initialExtra={false} />);
    expect(screen.getByRole("button", { name: /4 of 5/ })).toBeTruthy();
    const pop = open();
    fireEvent.click(within(pop).getByRole("button", { name: "Select all" }));
    expect(state()).toBe("all|true");
    expect(screen.getByRole("button", { name: /^All categories/ })).toBeTruthy();
  });

  it("leaves the extra alone when one item is ticked", () => {
    render(<Harness initialExtra={false} />);
    const pop = open();
    fireEvent.click(within(pop).getByRole("checkbox", { name: "Groceries" }));
    expect(state()).toBe("t,r,p|false");
  });
});
