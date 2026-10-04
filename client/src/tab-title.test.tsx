import { describe, expect, it } from "vitest";
import { menu, pageLabel, tabTitle } from "./App";

/** #189: every tab says which page it holds, in the menu's own words. */
describe("the tab title", () => {
  const sections = menu("Casa Doe");

  it("is the app's name, the page, then the household", () => {
    expect(tabTitle("Accounts", "Casa Doe")).toBe("Spend Tracker - Accounts - Casa Doe");
    expect(tabTitle("Accounts", "Flat 2")).toBe("Spend Tracker - Accounts - Flat 2");
    expect(tabTitle("Sign in")).toBe("Spend Tracker - Sign in");
    expect(tabTitle(null)).toBe("Spend Tracker");
  });

  it("keeps a household's name even when it matches a page's", () => {
    expect(tabTitle("Transfers", "Transfers")).toBe("Spend Tracker - Transfers - Transfers");
  });

  it("names a page as the menu does, at every depth", () => {
    expect(pageLabel(sections, "register")).toBe("Transactions");
    expect(pageLabel(sections, "rules")).toBe("Payee Naming Rules");
    expect(pageLabel(sections, "import-guide")).toBe("How import works");
    expect(pageLabel(sections, "reports")).toBe("Reports");
    expect(pageLabel(sections, "admin")).toBe("Admin");
  });

  it("names the household page after the household", () => {
    expect(pageLabel(sections, "household")).toBe("Casa Doe");
    expect(pageLabel(menu("Flat 2"), "household")).toBe("Flat 2");
  });

  it("names each report", () => {
    const reports = sections.find((one) => one.id === "reports")!;
    for (const child of reports.children) {
      if ("key" in child) expect(pageLabel(sections, child.key)).toBe(child.label);
    }
  });
});
