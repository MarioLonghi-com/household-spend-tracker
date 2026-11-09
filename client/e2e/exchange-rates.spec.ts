import { expect, test } from "@playwright/test";

/**
 * Browse rates on the Exchange rates page (#410), against a real server.
 *
 * A page of a hundred rates is taller than the window, so its headings have
 * to stay in view while it scrolls -- and whether they do depends on which
 * box the browser treats as the scroller, which only a browser can say. The
 * filters live in the address, so a reload opens the same list.
 *
 * The rates are entered by hand through the page's own import route: the
 * suite never reaches the ECB.
 */

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await page.getByRole("heading", { name: "Transactions", exact: true }).waitFor();
});

/** 150 days of pound rates entered by hand, ending last week. Idempotent. */
async function enterPoundRates(page: import("@playwright/test").Page): Promise<string> {
  return page.evaluate(async () => {
    const households = (await (await fetch("/api/households")).json()) as { id: string }[];
    const id = households[0].id;
    const lines = ["currency,date,rate"];
    const day = new Date(Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), new Date().getUTCDate() - 7));
    for (let i = 0; i < 150; i += 1) {
      const when = new Date(day.getTime() - i * 86_400_000).toISOString().slice(0, 10);
      lines.push(`GBP,${when},0.8${String(100 + i).padStart(3, "0")}`);
    }
    const form = new FormData();
    form.append("file", new Blob([lines.join("\n")], { type: "text/csv" }), "rates.csv");
    form.append("dry_run", "false");
    const answer = await fetch(`/api/households/${id}/fx/rates/manual/import`, { method: "POST", body: form });
    if (!answer.ok) throw new Error(`import refused: ${answer.status} ${await answer.text()}`);
    return id;
  });
}

test("Browse rates opens from its address, a page at a time, and its headings stay in view (#410)", async ({
  page,
}, testInfo) => {
  const household = await enterPoundRates(page);
  await page.goto(`/?open=exchange-rates&household=${household}&currency=GBP&source=manual`);

  const card = page.locator("section", { has: page.getByRole("heading", { name: "Browse rates" }) });
  await expect(card.getByLabel("Currency")).toHaveValue("GBP");
  await expect(card.getByLabel("Source")).toHaveValue("manual");
  await expect(card.getByText("1–100 of 150 rates")).toBeVisible();
  const rows = card.locator(".fx-browse-table tbody tr");
  await expect(rows).toHaveCount(100);

  // The next page is the server's, and the address follows it.
  await card.getByRole("button", { name: "Next page" }).first().click();
  await expect(card.getByText("101–150 of 150 rates")).toBeVisible();
  await expect(rows).toHaveCount(50);
  expect(new URL(page.url()).searchParams.get("page")).toBe("2");

  // A reload opens the same list.
  await page.reload();
  await expect(card.getByText("101–150 of 150 rates")).toBeVisible();
  await expect(card.getByLabel("Currency")).toHaveValue("GBP");

  // On a phone the table is a card per row and has no headings to keep.
  test.skip(testInfo.project.name !== "desktop", "the headings are hidden on a phone");
  await card.getByRole("button", { name: "First page" }).first().click();
  await expect(rows).toHaveCount(100);
  await rows.nth(70).scrollIntoViewIfNeeded();
  const main = (await page.locator("main.content").boundingBox())!;
  const heading = card.locator(".fx-browse-table thead th").first();
  const stuck = (await heading.boundingBox())!;
  // Flush with the top of what is being read, not scrolled away above it.
  expect(Math.abs(stuck.y - main.y)).toBeLessThan(2);
  await expect(heading).toBeInViewport();
  // And it is the rows that went under it.
  expect((await rows.nth(0).boundingBox())!.y).toBeLessThan(main.y);
});
