// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { ReportGrid, centreOn, gridSizes, latestFirst, monthsInView } from "./ReportGrid";

afterEach(cleanup);

const DESKTOP = gridSizes(1440);

describe("the months in view (#401)", () => {
  it("pins Total and Average only where months still fit beside them", () => {
    expect(gridSizes(1440).pinRight).toBe(true);
    expect(gridSizes(700).pinRight).toBe(false);
    expect(gridSizes(390)).toEqual({ label: 170, month: 104, summary: 112, pinRight: false });
  });

  it("counts the months between the pinned columns, from where the figures are scrolled", () => {
    // 220 label + 2 x 124 summaries leaves 696 px: six months of 116.
    expect(monthsInView(0, 1164, 12, DESKTOP)).toEqual({ first: 0, visible: 6 });
    expect(monthsInView(116 * 3, 1164, 12, DESKTOP)).toEqual({ first: 3, visible: 6 });
    // Never past the last month, however far the box says it scrolled.
    expect(monthsInView(99999, 1164, 12, DESKTOP)).toEqual({ first: 6, visible: 6 });
  });

  it("shows every month when the window holds them all", () => {
    expect(monthsInView(0, 5000, 4, DESKTOP)).toEqual({ first: 0, visible: 4 });
  });

  it("opens on the latest months, which on a phone is not the far end", () => {
    const phone = gridSizes(390);
    // 366 px of box, 170 of label: not quite two months of 104 beside it.
    const { visible } = monthsInView(0, 366, 12, phone);
    expect(latestFirst(12, visible, phone)).toBe((12 - visible) * 104);
    // Total and Average sit past that, off to the right, rather than in place of every month.
    expect(latestFirst(12, visible, phone) + 366).toBeLessThan(170 + 12 * 104 + 2 * 112);
  });

  it("centres a clicked month in the window, and never scrolls before the first", () => {
    expect(centreOn(6, 6, DESKTOP)).toBe((6 + 0.5 - 3) * 116);
    expect(centreOn(0, 6, DESKTOP)).toBe(0);
  });
});

describe("the frame", () => {
  const months = ["2026-08", "2026-09"];
  const mount = () =>
    render(
      <ReportGrid
        months={months}
        net={{ "2026-08": 100, "2026-09": -50 }}
        currency="EUR"
        head={(hidden) => (
          <>
            <th className="report-label" />
            {months.map((one) => (
              <th key={one} className="amount">{hidden ? <span className="sr-only">{one}</span> : one}</th>
            ))}
            <th className="amount">Total</th>
            <th className="amount">Average</th>
          </>
        )}
      >
        <tbody>
          <tr>
            <th className="report-label">Rent</th>
            <td className="amount">1</td>
            <td className="amount">2</td>
            <td className="amount">3</td>
            <td className="amount">1.5</td>
          </tr>
        </tbody>
      </ReportGrid>,
    );

  it("keeps the real heading inside the figures' table, and the drawn one out of the reading order", () => {
    mount();
    const tables = document.querySelectorAll("table.report");
    expect(tables).toHaveLength(2);
    expect(tables[0].closest('[aria-hidden="true"]')).not.toBeNull();
    const words = [...tables[1].querySelectorAll("thead .sr-only")].map((one) => one.textContent);
    expect(words).toEqual(["2026-08", "2026-09"]);
    expect(tables[1].querySelector("tbody th")?.textContent).toBe("Rent");
  });

  it("gives both tables the same columns, so a month is as wide in each", () => {
    mount();
    const widths = [...document.querySelectorAll("table.report")].map((table) =>
      [...table.querySelectorAll("col")].map((col) => (col as HTMLElement).style.width),
    );
    expect(widths[0]).toEqual(widths[1]);
    expect(widths[0]).toHaveLength(months.length + 3);
  });

  it("draws no month map when everything fits -- there is nowhere to go", () => {
    mount();
    expect(document.querySelector(".ie-map")).toBeNull();
  });
});
