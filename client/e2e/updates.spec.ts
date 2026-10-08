import { expect, test } from "@playwright/test";
import { go } from "./helpers";

/**
 * The Updates section and the update backups (#166), on the suite's own
 * ledger: `serve.sh` runs it as a checkout, and `bootstrap.py` leaves seven
 * update backups behind, each named by a history record as an updater would.
 *
 * The real update, end to end, is the self-update CI job's (#169), not this.
 */

test.beforeEach(async ({ page }) => {
  // Signed in by the `setup` project's stored state, as in app.spec.ts.
  await page.goto("/");
  await page.getByRole("heading", { name: "Transactions", exact: true }).waitFor();
});

test("a checkout says how to update from the terminal, and offers no Prepare", async ({ page }) => {
  // What the server itself says this instance is.
  // Asked from the page, as the screen asks: the session is the browser's.
  const said = await page.evaluate(async () => {
    const answer = await fetch("/api/admin/application/update", { credentials: "same-origin" });
    return { status: answer.status, body: await answer.json() };
  });
  expect(said.status).toBe(200);
  expect(said.body.case).toBe("not_container");

  // The check reaches GitHub, which a test must not depend on: answer it here
  // with two newer releases, one of whose notes is markup.
  await page.route("**/api/admin/application/upstream", (route) =>
    route.fulfill({
      json: {
        checked_at: "2026-10-08T10:00:00Z",
        running: "0.8.0",
        latest: "0.10.0",
        newer: true,
        problem: null,
        releases: [
          {
            version: "0.10.0",
            tag: "v0.10.0",
            name: null,
            published_at: "2026-10-07T09:00:00Z",
            notes: "<b>Added</b>: receipts by date.",
            notes_from: "changelog",
          },
          {
            version: "0.9.0",
            tag: "v0.9.0",
            name: null,
            published_at: "2026-10-01T09:00:00Z",
            notes: "Changed: theme colours.",
            notes_from: "changelog",
          },
        ],
        updater: { version: null, compatible: null, note: "No published release is newer than this one." },
      },
    }),
  );

  await go(page, "Application management");
  const section = page.locator("section", { has: page.getByRole("heading", { name: "Updates", exact: true }) });
  await expect(section).toBeVisible();
  await section.getByRole("button", { name: "Check the repository" }).click();

  await expect(section.getByText("0.10.0 is available.")).toBeVisible();
  await expect(section.locator('[data-case="not_container"]')).toContainText(
    "This instance runs from a checkout. Update it from the terminal: make upgrade-check, then make upgrade.",
  );
  await expect(section.getByRole("button", { name: /^Prepare/ })).toHaveCount(0);
  // Notes are text: the tag arrives as characters.
  const notes = section.locator('.release-notes[data-version="0.10.0"]');
  await expect(notes).toHaveText("<b>Added</b>: receipts by date.");
  await expect(notes.locator("b")).toHaveCount(0);
});

test("update backups list with their version, the newest five kept, and an older one deletes", async ({
  page,
}, testInfo) => {
  await go(page, "Application management");
  const block = page.locator(".update-backups");
  await expect(block.getByRole("heading", { name: "Backups taken by updates" })).toBeVisible();
  const rows = block.locator("tbody tr");
  // Seven seeded; the desktop pass deletes one, and the phone pass may run after it.
  expect(await rows.count()).toBeGreaterThanOrEqual(6);
  await expect(block.locator('tbody tr[data-protected="true"]')).toHaveCount(5);
  for (const row of await block.locator('tbody tr[data-protected="true"]').all()) {
    await expect(row.getByRole("button", { name: "Delete" })).toHaveCount(0);
    await expect(row.getByText("kept", { exact: true })).toBeVisible();
  }
  const newest = rows.first();
  await expect(newest.locator("td[data-primary]")).toHaveText("20260925-100000");
  await expect(newest).toContainText("0.8.0");
  await expect(newest.getByRole("link", { name: "Download" })).toHaveAttribute(
    "href",
    /\/api\/admin\/application\/backups\/20260925-100000\/download/,
  );
  // Update backups are not repeated in the list of backups made by hand.
  await expect(page.locator("tbody tr", { hasText: "20260925-100000" })).toHaveCount(1);

  // The server refuses the newest five whatever the client sends.
  const refused = await page.evaluate(async () => {
    const answer = await fetch("/api/admin/application/backups/20260925-100000", {
      method: "DELETE",
      credentials: "same-origin",
    });
    return { status: answer.status, body: await answer.json() };
  });
  expect(refused.status).toBe(409);
  expect(refused.body.code).toBe("backup.protected");
  await page.reload();
  await page.getByRole("heading", { name: "Transactions", exact: true }).waitFor();
  await go(page, "Application management");
  await expect(block.locator("tbody tr", { hasText: "20260925-100000" })).toHaveCount(1);

  if (testInfo.project.name === "mobile") {
    const overflows = await block
      .locator(".table-scroll table")
      .evaluate((node) => node.scrollWidth > node.clientWidth + 1);
    expect(overflows).toBe(false);
    return;
  }

  // Sorted at its headings, by version as numbers.
  const version = block.locator("th.sortable", { hasText: "Version" });
  await version.getByRole("button").click();
  await expect(rows.first()).toContainText("0.2.0");

  const oldest = block.locator("tbody tr", { hasText: "20260901-100000" });
  await oldest.getByRole("button", { name: "Delete" }).click();
  const dialog = page.getByRole("dialog", { name: "Delete this update backup?" });
  await expect(dialog).toContainText("20260901-100000");
  await dialog.getByRole("button", { name: "Yes, delete it" }).click();
  await expect(block.locator("tbody tr", { hasText: "20260901-100000" })).toHaveCount(0);
  await expect(block.locator('tbody tr[data-protected="true"]')).toHaveCount(5);
});
