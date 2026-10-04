/**
 * The work-expense state and the grouped register order.
 *
 * Every input the two columns can hold, including the two the server's rules
 * make impossible: the client has to be defined for them anyway, because the
 * pill is drawn on every row of the register and one that throws takes the
 * whole screen with it.
 */

import { describe, expect, it } from "vitest";
import { WORK_PILLS, ageInDays, groupByPayment, workState } from "./reimbursement";

describe("workState", () => {
  it.each([
    [{ reimbursement: null, reimbursed_by_id: null }, "none"],
    [{ reimbursement: "expected", reimbursed_by_id: null }, "owed"],
    [{ reimbursement: "expected", reimbursed_by_id: "pay-1" }, "paid"],
    [{ reimbursement: "written_off", reimbursed_by_id: null }, "off"],
  ] as const)("reads %j as %s", (txn, state) => {
    expect(workState(txn)).toBe(state);
  });

  it("reads a written-off row with a link as written off -- the state wins", () => {
    expect(workState({ reimbursement: "written_off", reimbursed_by_id: "pay-1" })).toBe("off");
  });

  it("reads a link on an unflagged row as an ordinary row", () => {
    expect(workState({ reimbursement: null, reimbursed_by_id: "pay-1" })).toBe("none");
  });

  it("draws nothing for a row from an endpoint that sends neither column", () => {
    expect(workState({})).toBe("none");
  });

  it("draws nothing for a state nobody designed", () => {
    expect(
      workState({ reimbursement: "submitted" as never, reimbursed_by_id: "pay-1" }),
    ).toBe("none");
  });

  it("gives each drawn state its own class and its own words", () => {
    const classes = Object.values(WORK_PILLS).map((one) => one.className);
    const words = Object.values(WORK_PILLS).map((one) => one.word);
    expect(new Set(classes).size).toBe(3);
    expect(new Set(words).size).toBe(3);
    expect(WORK_PILLS.owed.className).toBe("tag work-owed");
    expect(WORK_PILLS.paid.word).toBe("Work expense — reimbursed");
  });
});

describe("ageInDays", () => {
  it("counts across a month boundary", () => {
    expect(ageInDays("2026-08-30", "2026-09-02")).toBe(3);
  });

  it("counts whole days across a daylight-saving change", () => {
    // Europe changes on the last Sunday of March and of October.
    expect(ageInDays("2026-03-28", "2026-03-30")).toBe(2);
    expect(ageInDays("2026-10-24", "2026-10-26")).toBe(2);
  });

  it("is negative for a payment that came first -- an advance", () => {
    expect(ageInDays("2026-09-10", "2026-09-01")).toBe(-9);
  });

  it("is zero for a date it cannot read rather than NaN", () => {
    expect(ageInDays("", "2026-09-01")).toBe(0);
  });
});

describe("groupByPayment", () => {
  const row = (id: string, date: string, reimbursed_by_id: string | null = null) => ({
    id,
    date,
    reimbursed_by_id,
  });

  it("puts each payment's expenses under it, oldest first, and leaves the rest in order", () => {
    const rows = [
      row("pay-2", "2026-10-30"),
      row("taxi", "2026-10-20", "pay-2"),
      row("pay-1", "2026-09-30"),
      row("loose", "2026-09-25"),
      row("hotel", "2026-09-12", "pay-1"),
      row("train", "2026-09-03", "pay-1"),
    ];
    expect(groupByPayment(rows).map(({ row, child }) => `${child ? "↳" : ""}${row.id}`)).toEqual([
      "pay-2",
      "↳taxi",
      "pay-1",
      "↳train",
      "↳hotel",
      "loose",
    ]);
  });

  it("does not indent an expense whose payment is not on screen", () => {
    const grouped = groupByPayment([row("a", "2026-09-01", "elsewhere"), row("b", "2026-09-02")]);
    expect(grouped.map((one) => [one.row.id, one.child])).toEqual([
      ["a", false],
      ["b", false],
    ]);
  });

  it("keeps every row of a cycle on screen rather than nesting it away", () => {
    const grouped = groupByPayment([
      row("a", "2026-09-01", "b"),
      row("b", "2026-09-02", "a"),
      row("self", "2026-09-03", "self"),
    ]);
    expect(grouped.map((one) => one.row.id).sort()).toEqual(["a", "b", "self"]);
    expect(grouped.every((one) => !one.child)).toBe(true);
  });
});
