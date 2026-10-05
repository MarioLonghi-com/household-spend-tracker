import { expect, test } from "@playwright/test";
import { go } from "./helpers";

/**
 * End to end, against the built client and a real server with a real ledger.
 *
 * Every test here runs twice: once as a desktop browser and once as an iPhone.
 * That is the point of the file. Half of what it covers only exists at one
 * width -- the nav is a drawer on a phone and a sidebar on a desktop -- and
 * the phone layout was unusable for the life of the build without a single
 * test noticing, because nothing had ever looked at it.
 */

test.beforeEach(async ({ page }) => {
  // Already signed in: the session comes from the `setup` project's stored
  // state, because a TOTP code cannot be presented twice.
  await page.goto("/");
  await page.getByRole("heading", { name: "Transactions", exact: true }).waitFor();
});

test("the register is the screen, and entry is a panel off the top", async ({ page }) => {
  // Both ways in are in the header now, behind one button (#143). They used
  // to be a card pinned under the register that cost the rows a third of the
  // screen permanently, and then two buttons side by side.
  const add = page.getByRole("button", { name: "Add transaction" });
  await expect(add).toBeVisible();
  await expect(page.getByRole("button", { name: "Add transfer", exact: true })).toHaveCount(0);

  // And the form itself is not on the screen until it is asked for.
  await expect(page.getByRole("heading", { name: "Add a transaction" })).toHaveCount(0);

  await add.click();
  await page.getByRole("menuitem", { name: "Single transaction" }).click();
  await expect(page.getByRole("heading", { name: "Add a transaction" })).toBeVisible();
  // Scoped to the panel: every row behind it has a "— edit the payee" button.
  await expect(page.locator(".panel").getByLabel("Payee")).toBeVisible();

  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.getByRole("heading", { name: "Add a transaction" })).toHaveCount(0);

  await add.click();
  await page.getByRole("menuitem", { name: "Transfer" }).click();
  await expect(page.getByRole("heading", { name: "Add transfer between accounts" })).toBeVisible();
});

test("a split opens already divided evenly, and stays balanced as parts are added", async ({
  page,
}) => {
  await openSplit(page);

  // Two parts, equal, and the panel says so before anybody types. It used to
  // open with the whole amount on part one and part two empty, so its opening
  // words were "one part is still empty".
  await expect(page.getByText("It adds up")).toBeVisible();
  const values = await amounts(page);
  expect(values).toHaveLength(2);
  // 50/50, and neither box empty -- the panel used to open with the whole
  // amount on part one and nothing on part two.
  //
  // "Equal" is within a cent, not identical: an odd total cannot divide two
  // ways exactly, and the remainder goes to the last part on purpose. 6.23
  // becomes 3.11 and 3.12, which adds up; two of 3.11 does not.
  expect(values[0]).not.toBe("");
  expectWithinACent(values);

  // Adding a part re-divides rather than leaving a remainder to work out.
  await page.getByRole("button", { name: "Add a part" }).click();
  await expect(page.getByText("It adds up")).toBeVisible();
  const three = await amounts(page);
  expect(three).toHaveLength(3);
  // 33.33 / 33.33 / 33.34, not three of 33.33 with a cent unaccounted for.
  expectWithinACent(three);

  await page.getByRole("button", { name: "Add a part" }).click();
  await expect(page.getByText("It adds up")).toBeVisible();
  expect(await amounts(page)).toHaveLength(4);
});

test("the split panel offers real categories", async ({ page }) => {
  await openSplit(page);

  // The household starts with the default tree, so this select has groups in
  // it. When it had none the only option was "Uncategorised", which read as a
  // broken picker rather than an empty ledger.
  const category = page.locator(".split-part select").first();
  await expect(category).toBeVisible();
  const groups = await category.locator("optgroup").count();
  expect(groups).toBeGreaterThan(0);
  await expect(category.locator("option", { hasText: "Groceries" })).toHaveCount(1);
});

test("splitting a transaction shows up in History, naming what was split", async ({ page }) => {
  const before = await openSplit(page);

  await page.locator(".split-part select").first().selectOption({ label: "Groceries" });
  await page.getByRole("button", { name: /^Split into/ }).click();

  // The panel closes and the register is back.
  await expect(page.getByRole("heading", { name: "Transactions", exact: true })).toBeVisible();

  await go(page, "History");
  await expect(page).toHaveTitle(/^Spend Tracker - History - .+/);
  const row = page.getByText("Split", { exact: true }).first();
  await expect(row).toBeVisible();

  // The original is named. The generic sentence for a multi-row batch said
  // "3 transactions changed", which names nothing -- and a split is the one
  // act whose subject no longer exists by the time you read about it.
  await expect(page.getByText(before.payee, { exact: false }).first()).toBeVisible();
  await expect(page.getByText("split into 2", { exact: false }).first()).toBeVisible();
});

test("`/snap` is reachable from the nav rather than only by knowing the URL", async ({ page }) => {
  const link = page.getByRole("link", { name: "Snap a Receipt" });
  // On a phone it lives in the drawer.
  const menu = page.getByRole("button", { name: /Open the menu/ });
  if (await menu.isVisible()) await menu.click();

  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute("href", "/snap");

  // In a tab of its own, so the ledger stays where it was.
  const opened = page.context().waitForEvent("page");
  await link.click();
  const snap = await opened;
  await snap.waitForLoadState();
  await expect(snap).toHaveURL(/\/snap$/);
  await expect(page).not.toHaveURL(/\/snap$/);
  // It is its own page outside the SPA, and it loaded rather than redirecting
  // back to the sign-in screen.
  await expect(snap.locator("body")).not.toHaveText(/Sign in/);
});

/**
 * #184: a group's heading opens it, to rename it or delete it while it holds
 * nothing. The delete is a hard one, so History's Undo is the way back -- and
 * that is what this proves, rather than that a button was pressed.
 */
test("a category group is renamed and deleted from its heading, and Undo brings it back", async ({
  page,
}, testInfo) => {
  // Both projects share one ledger, so each works on a group of its own.
  const first = `Spare ${testInfo.project.name}`;
  const renamed = `Kept aside ${testInfo.project.name}`;
  const main = page.locator("main");
  const panel = page.locator(".panel");
  const heading = (name: string) => main.getByRole("button", { name, exact: true });

  await go(page, "Categories");

  // A group with categories under it stays, and the alert says why.
  await heading("Everyday").click();
  await panel.getByRole("button", { name: "Delete group" }).click();
  // #199: a delete asks first, and the refusal is said in the question.
  const confirm = page.getByRole("dialog", { name: /^Delete the group / });
  await confirm.getByRole("button", { name: "Yes, delete it" }).click();
  await expect(confirm.getByRole("alert")).toContainText(
    "can only be deleted when there are no categories under it",
  );
  await confirm.getByRole("button", { name: "Keep it" }).click();
  await panel.getByRole("button", { name: "Close" }).click();
  await expect(heading("Everyday")).toBeVisible();

  await main.getByRole("button", { name: "Add a group" }).click();
  await panel.getByLabel("Name").fill(first);
  await panel.getByRole("button", { name: "Create" }).click();
  await expect(heading(first)).toBeVisible();

  await heading(first).click();
  await panel.getByLabel("Name").fill(renamed);
  await panel.getByRole("button", { name: "Save" }).click();
  await expect(heading(renamed)).toBeVisible();
  await expect(heading(first)).toHaveCount(0);

  await heading(renamed).click();
  await panel.getByRole("button", { name: "Delete group" }).click();
  await page.getByRole("button", { name: "Yes, delete it" }).click();
  await expect(panel).toHaveCount(0);
  await expect(heading(renamed)).toHaveCount(0);

  await go(page, "History");
  await main.getByRole("button", { name: new RegExp(`Removed category group ${renamed}`) }).click();
  await panel.getByRole("button", { name: "Undo this" }).click();
  await panel.getByRole("button", { name: "Yes, undo it" }).click();
  await expect(panel).toHaveCount(0);

  await go(page, "Categories");
  await expect(heading(renamed)).toBeVisible();
});

/** One transaction, opened and sent to the split panel. Returns what it was. */
async function openSplit(page: import("@playwright/test").Page) {
  const row = page
    .locator("tbody tr")
    .filter({ hasNot: page.locator("text=Transfer :") })
    .first();
  // By its label, not its position: the tick box and the account come first,
  // and `nth(2)` was the account all along -- it only passed while the newest
  // row's account happened to appear in History too.
  const payee = (await row.locator('td[data-label="Payee"]').innerText()).trim();
  await row.locator("td a, td button").first().click();
  await page.getByRole("button", { name: "Split", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Split this transaction" })).toBeVisible();
  return { payee };
}

/**
 * Every part is the same size, to the cent the currency can express.
 *
 * Not identical: a total that does not divide exactly cannot produce identical
 * parts *and* add up, and adding up is the property the service enforces. So
 * the test is that nothing is more than one minor unit from anything else.
 */
function expectWithinACent(values: string[]): void {
  const numbers = values.map((one) => Number(one.replace(",", ".")));
  expect(numbers.every((one) => Number.isFinite(one))).toBe(true);
  expect(Math.max(...numbers) - Math.min(...numbers)).toBeLessThanOrEqual(0.011);
}

/** What each part's amount box currently reads. */
async function amounts(page: import("@playwright/test").Page): Promise<string[]> {
  // The amount box is the one that takes a decimal keyboard; the other input
  // in each part is its memo.
  return page
    .locator(".split-part input[inputmode='decimal']")
    .evaluateAll((nodes) => nodes.map((one) => (one as HTMLInputElement).value));
}

test("both entry surfaces offer a category, and the same one the list does", async ({ page }) => {
  // The add form.
  await page.getByRole("button", { name: "Add transaction" }).click();
  await page.getByRole("menuitem", { name: "Single transaction" }).click();
  // Exact: the register's category filter has the word in its ticks too
  // ("Needs a category"), and a substring match finds both.
  const adding = page.getByLabel("Category", { exact: true });
  await expect(adding).toBeVisible();
  // Typing part of a name is how the register's own cell works; the panel
  // must not be a dropdown while the list is a typeahead.
  await adding.fill("groc");
  await expect(page.getByRole("option", { name: /Groceries/ }).first()).toBeVisible();
  await page.getByRole("button", { name: "Close" }).click();

  // And the detail panel.
  await openRow(page);
  await expect(page.getByLabel("Category", { exact: true })).toBeVisible();
});

test("the detail panel saves on the way out of a field", async ({ page }) => {
  await openRow(page);

  // Scoped to the panel throughout: the register behind it gives every row an
  // "edit the memo" and an "edit the payee" button of its own.
  const panel = page.locator(".panel");
  const memo = panel.getByLabel("Memo");
  const written = `checked ${Date.now()}`;
  await memo.fill(written);
  // Leaving the field is the save. There is no Save button any more.
  await panel.getByLabel("Payee").click();
  await expect(page.getByRole("button", { name: "Done" })).toBeVisible();

  // Prove it reached the server rather than only the input: close, reopen.
  await page.getByRole("button", { name: "Done" }).click();
  await openRow(page);
  await expect(page.locator(".panel").getByLabel("Memo")).toHaveValue(written);
});

test("the date and the amount will not be edited by a stray keystroke", async ({ page }) => {
  await openRow(page);

  // Shown, not presented for editing: there is no date input until somebody
  // asks for one. These two decide a balance, and the panel saves on blur.
  await expect(page.locator('input[type="date"]')).toHaveCount(0);

  // Scoped to the panel: the column heading is also a button called "Date".
  const guard = page.locator(".panel").getByRole("button", { name: /^Date — / });
  await expect(guard).toBeVisible();
  await guard.dblclick();
  await expect(page.locator('input[type="date"]')).toHaveCount(1);

  // And the amount is behind the same guard, for the same reason.
  await expect(
    page.locator(".panel").getByRole("button", { name: /^Amount/ }),
  ).toBeVisible();
});

test("the snap page says who and where, and offers a way back", async ({ page }) => {
  await page.goto("/snap");

  // Two accounts in one browser look identical otherwise, and so do two
  // households -- which is how a receipt lands in the wrong ledger.
  await expect(page.locator("#who")).not.toBeEmpty();
  await expect(page.locator("#where")).not.toBeEmpty();

  const back = page.locator("#toapp");
  await expect(back).toHaveAttribute("href", "/");

  // The library picker exists alongside the shutter: `capture` takes one photo
  // however many the person has on the table.
  await expect(page.locator("#several")).toBeVisible();

  // The household list is a popup, not furniture. `display: grid` used to beat
  // the user agent's `[hidden]`, so it was on screen from load.
  await expect(page.locator("#switcher")).toBeHidden();
});

/** Open the first ordinary row's detail panel. */
async function openRow(page: import("@playwright/test").Page) {
  const row = page
    .locator("tbody tr")
    .filter({ hasNot: page.locator("text=Transfer :") })
    .first();
  await row.locator("td a, td button").first().click();
  await page.getByRole("button", { name: "Done" }).waitFor();
}

test("on a phone the card encloses its rows, and the filters start out of the way", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "this is the phone layout");

  // Wait for rows before measuring. This is a geometry assertion and geometry
  // is only meaningful once there is something laid out: the run that flaked
  // measured a card whose rows had not arrived, and a height taken mid-render
  // is not a fact about the layout.
  await page.locator(".register-card tbody tr").nth(5).waitFor();

  // The reported bug, as a measurement. `.screen-fill` is a flex column sized
  // to the window and `.register-card` was `flex: 1` inside it, so the card
  // was 665px tall while the table it contains was 14,723 -- the rows drew
  // straight past the bottom of the white surface they sit on.
  const geometry = await page.evaluate(() => {
    const card = document.querySelector(".register-card")!;
    const scroller = card.querySelector(".table-scroll")!;
    const rows = scroller.querySelectorAll("tbody tr");
    const last = rows[rows.length - 1];
    return {
      cardBottom: Math.round(card.getBoundingClientRect().bottom),
      lastRowBottom: Math.round(last.getBoundingClientRect().bottom),
      rows: rows.length,
      horizontalOverflow:
        document.documentElement.scrollWidth > document.documentElement.clientWidth,
    };
  });

  expect(geometry.rows).toBeGreaterThan(5);
  expect(geometry.cardBottom).toBeGreaterThanOrEqual(geometry.lastRowBottom);
  // And nothing scrolls sideways: that was what the card layout replaced.
  expect(geometry.horizontalOverflow).toBe(false);

  // The filters are put away until asked for -- 225px of controls before the
  // first transaction is a quarter of a phone screen.
  const filters = page.locator(".filters");
  await expect(filters).toBeHidden();
  const toggle = page.getByRole("button", { name: "Filters" });
  await toggle.click();
  await expect(filters).toBeVisible();
  await page.getByRole("button", { name: "Hide filters" }).click();
  await expect(filters).toBeHidden();
});

test("the desktop register is still a table", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "this is the wide layout");

  // None of the phone machinery exists here: no menu button, no filters
  // toggle, and the filters are simply on screen.
  await expect(page.getByRole("button", { name: "Filters" })).toHaveCount(0);
  await expect(page.locator(".filters")).toBeVisible();
  await expect(page.locator("thead")).toBeVisible();
});

test("the filter line's boxes are one height and its labels one line, in one typeface", async ({
  page,
}, testInfo) => {
  // #145. After #143 the accounts pill was 38.5px tall at 15px, Search and
  // Amount 35.5px at 13px, and the line aligns bottoms -- so the Accounts
  // label stood 3px above "Search" beside it. Measured, not looked at: a
  // pixel or three is exactly what an eye skims past in a screenshot.
  const phone = testInfo.project.name === "mobile";
  await page.locator(".register-card tbody tr").nth(3).waitFor();
  if (phone) await page.getByRole("button", { name: "Filters" }).click();
  const line = page.locator(".register-card .filters .filter-line");
  await expect(line).toBeVisible();

  type Box = { top: number; bottom: number; height: number };
  type Measured = { name: string; label: Box; box: Box; boxFont: number; placeholderFont: number | null };
  const measure = () =>
    line.evaluate((node): Measured[] => {
      const box = (el: Element): Box => {
        const r = el.getBoundingClientRect();
        return { top: r.top, bottom: r.bottom, height: r.height };
      };
      return [...node.querySelectorAll(":scope > .field")].map((field) => {
        const label = field.querySelector(":scope > .field-head, :scope > span")!;
        const control = field.querySelector(
          ":scope > input, :scope > select, :scope > .picker > .chip",
        )!;
        return {
          name: label.textContent!.replace(/\s+/g, " ").trim(),
          label: box(label),
          box: box(control),
          boxFont: parseFloat(getComputedStyle(control).fontSize),
          placeholderFont:
            control instanceof HTMLInputElement
              ? parseFloat(getComputedStyle(control, "::placeholder").fontSize)
              : null,
        };
      });
    });

  const fields = await measure();
  const named = (prefix: string) => {
    const found = fields.find((one) => one.name.startsWith(prefix));
    expect(found, `a filter labelled ${prefix}`).toBeDefined();
    return found!;
  };
  const accounts = named("Accounts");
  const search = named("Search");
  const amount = named("Amount");

  // Every box on the line is one height, and every label is one height and
  // sits the same distance above its box -- at either width, whether the
  // fields share a row or stack.
  for (const one of fields) {
    expect(Math.abs(one.box.height - search.box.height), `${one.name} box height`).toBeLessThanOrEqual(1);
    expect(Math.abs(one.label.height - search.label.height), `${one.name} label height`).toBeLessThanOrEqual(1);
    expect(
      Math.abs(one.box.top - one.label.top - (search.box.top - search.label.top)),
      `${one.name} label-to-box`,
    ).toBeLessThanOrEqual(1);
  }
  // And any two that share a row share a label line and a box line: the
  // failure was a row whose members stood at different heights.
  for (const one of fields) {
    for (const other of fields) {
      const sameRow = one.box.top < other.box.bottom && other.box.top < one.box.bottom;
      if (!sameRow) continue;
      expect(Math.abs(one.label.top - other.label.top), `${one.name} / ${other.name} labels`).toBeLessThanOrEqual(1);
      expect(Math.abs(one.box.top - other.box.top), `${one.name} / ${other.name} boxes`).toBeLessThanOrEqual(1);
    }
  }

  if (!phone) {
    // Accounts and Search are side by side, and Amount beside them: one
    // label line and one box line for the three.
    for (const one of [search, amount]) {
      expect(Math.abs(one.label.top - accounts.label.top)).toBeLessThanOrEqual(1);
      expect(Math.abs(one.box.top - accounts.box.top)).toBeLessThanOrEqual(1);
      expect(Math.abs(one.box.height - accounts.box.height)).toBeLessThanOrEqual(1);
    }
    // One smaller size for the boxes, the pill and the placeholders -- the
    // pill was 15px.
    for (const one of [accounts, search, amount]) expect(one.boxFont, one.name).toBe(13);
    expect(search.placeholderFont).toBe(13);
    expect(amount.placeholderFont).toBe(13);

    // The table's text size is the table's: stepping it moves nothing here.
    const before = await measure();
    const tableText = () =>
      page.locator(".register-card table").evaluate((el) => getComputedStyle(el).fontSize);
    const startedAt = await tableText();
    await page.getByRole("button", { name: "Larger text in the table" }).click();
    await expect.poll(tableText).not.toBe(startedAt);
    expect(await measure()).toEqual(before);
    await page.getByRole("button", { name: "Smaller text in the table" }).click();
    await page.getByRole("button", { name: "Smaller text in the table" }).click();
    await expect.poll(tableText).not.toBe(startedAt);
    expect(await measure()).toEqual(before);
  } else {
    // Stacked: Search starts below the accounts pill, not beside it.
    expect(search.label.top).toBeGreaterThanOrEqual(accounts.box.bottom);
    // iOS zooms into anything under 16px when it is focused, selects
    // included, and the date range's selects are in the same drawer.
    for (const one of fields) {
      expect(one.boxFont, one.name).toBeGreaterThanOrEqual(16);
      if (one.placeholderFont !== null) expect(one.placeholderFont, one.name).toBeGreaterThanOrEqual(16);
    }
    const dateSelects = await page
      .locator(".register-card .filters .daterange-exact select")
      .evaluateAll((all) => all.map((el) => parseFloat(getComputedStyle(el).fontSize)));
    expect(dateSelects.length).toBeGreaterThan(0);
    for (const size of dateSelects) expect(size).toBeGreaterThanOrEqual(16);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
      ),
    ).toBe(false);
  }
});

test("a column is as wide as you drag it, and a row stays one line", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "there are no headings to drag on a phone");

  // #74. Rows first: the heading row is only measured once there is a table.
  await page.locator(".register-card tbody tr").nth(5).waitFor();
  const table = page.locator(".register-card table");
  const heading = (name: string) => page.locator(".register-card thead th", { hasText: name });
  const edge = (name: string) => page.getByRole("separator", { name: `Width of the ${name} column` });
  const widthOf = async (name: string) =>
    heading(name).evaluate((node) => Math.round(node.getBoundingClientRect().width));
  const rowHeights = async () =>
    page
      .locator(".register-card tbody tr")
      .evaluateAll((rows) => [...new Set(rows.map((row) => Math.round(row.getBoundingClientRect().height)))]);

  // Untouched, the browser lays the table out as it always has.
  await expect(table).not.toHaveClass(/sized/);
  const [lineHeight] = await rowHeights();

  // Drag the payee's right-hand edge 150px to the right.
  const payeeBefore = await widthOf("Payee");
  const accountBefore = await widthOf("Account");
  const box = (await edge("Payee").boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 75, box.y + box.height / 2, { steps: 5 });
  await page.mouse.move(box.x + box.width / 2 + 150, box.y + box.height / 2, { steps: 5 });
  await page.mouse.up();

  await expect(table).toHaveClass(/sized/);
  expect(Math.abs((await widthOf("Payee")) - (payeeBefore + 150))).toBeLessThanOrEqual(2);
  // The neighbour it did not touch stays where the browser had put it.
  expect(Math.abs((await widthOf("Account")) - accountBefore)).toBeLessThanOrEqual(2);

  // Squeeze the memo to almost nothing: its text is cut, not wrapped.
  const memo = (await edge("Memo").boundingBox())!;
  await page.mouse.move(memo.x + memo.width / 2, memo.y + memo.height / 2);
  await page.mouse.down();
  await page.mouse.move(memo.x - 400, memo.y + memo.height / 2, { steps: 5 });
  await page.mouse.up();
  expect(await widthOf("Memo")).toBeLessThanOrEqual(40);
  // Every row is one height, and it is the height a row had before.
  expect(await rowHeights()).toEqual([lineHeight]);

  // A keyboard moves the same edge, ten pixels an arrow.
  const payeeDragged = await widthOf("Payee");
  await edge("Payee").focus();
  await page.keyboard.press("ArrowRight");
  expect(Math.abs((await widthOf("Payee")) - (payeeDragged + 10))).toBeLessThanOrEqual(2);

  // Kept for the next visit on this device.
  await page.reload();
  await page.locator(".register-card tbody tr").nth(5).waitFor();
  await expect(table).toHaveClass(/sized/);
  expect(Math.abs((await widthOf("Payee")) - (payeeDragged + 10))).toBeLessThanOrEqual(2);
  if (process.env.SHOT) await page.screenshot({ path: process.env.SHOT });

  // Double-click hands the layout back to the browser, and forgets the widths.
  await edge("Payee").dblclick();
  await expect(table).not.toHaveClass(/sized/);
  expect(Math.abs((await widthOf("Payee")) - payeeBefore)).toBeLessThanOrEqual(2);
  expect(
    await page.evaluate(() => window.localStorage.getItem("spendtracker.register.columns")),
  ).toBeNull();
});

test("a phone held sideways still has a register", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "one width is enough for this");

  // 812x375 is a phone in landscape. It is wider than the card breakpoint, so
  // it gets the ordinary table -- and that is fine. What is not fine is the
  // height: `.screen-fill` is a flex column sized to the window with
  // `.register-card` flex: 1 inside it, so once the filters take their fixed
  // 225px there is nothing left, the table's flex child resolves to zero, and
  // the overflow is hidden. The register was not scrolling badly, it was
  // absent.
  await page.setViewportSize({ width: 812, height: 375 });
  await page.goto("/");
  await page.getByRole("heading", { name: "Transactions", exact: true }).waitFor();

  const rows = page.locator("tbody tr");
  await expect(rows.first()).toBeVisible();
  expect(await rows.count()).toBeGreaterThan(5);

  const height = await page
    .locator(".register-card .table-scroll")
    .evaluate((node) => Math.round(node.getBoundingClientRect().height));
  expect(height).toBeGreaterThan(100);
});

test("the accounts filter opens over the register, not under the sidebar", async ({
  page,
}, testInfo) => {
  // #160: the popover hung off the right edge of its button, and the button is
  // at the left of the filter line, so it opened leftwards out of the content
  // and under the sidebar. Measured against `main.content` rather than the
  // window, because on a desktop the sidebar is on the page beside it.
  if (testInfo.project.name === "mobile") {
    await page.getByRole("button", { name: "Filters" }).click();
  }
  await page.getByRole("button", { name: "All accounts" }).click();
  const pop = page.getByRole("group", { name: "All accounts" });
  await expect(pop).toBeVisible();

  const box = (await pop.boundingBox())!;
  const content = (await page.locator("main.content").boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(content.x);
  expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  // Nothing is drawn over it: the point in its middle is the popover's own.
  const onTop = await pop.evaluate((node) => {
    const r = node.getBoundingClientRect();
    const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    return !!hit && node.contains(hit);
  });
  expect(onTop).toBe(true);
});

test("the selection banner is in view after ticking a row far down the list", async ({
  page,
}) => {
  // #144: the banner with the sums and the bulk acts is pinned to the bottom
  // of the screen while there is a selection. #143 had it above the table, so
  // ticking a row a long way down left the sums and the acts a long scroll
  // back up. Both widths, because the pinning is done two different ways: on
  // a desktop the card is a window-high flex column and the dock is its last
  // item; on a phone the page scrolls and the dock is sticky at its bottom.
  const rows = page.locator(".register-card tbody tr");
  await rows.nth(40).waitFor();
  const far = rows.nth(40);
  await far.scrollIntoViewIfNeeded();
  // Clicked, not `check()`ed: the dock arriving can land over the row on a
  // phone, and that is the dock doing its job, not the tick failing.
  await far.getByRole("checkbox").click();

  const dock = page.locator(".register-card > .selection-dock");
  const banner = dock.locator(".selection-banner");
  await expect(banner).toContainText("1 selected.");
  await expect(banner).toBeInViewport();
  // Pinned to the bottom edge, and entirely on screen, not merely touching it.
  const viewport = page.viewportSize()!;
  const box = (await dock.boundingBox())!;
  expect(box.y).toBeGreaterThan(viewport.height / 3);
  expect(Math.round(box.y + box.height)).toBeLessThanOrEqual(viewport.height);
  // Its height is capped, so the rows still have most of the screen.
  expect(box.height).toBeLessThanOrEqual(viewport.height * 0.45 + 1);
  // The count line stays above the rows, and still counts the selection.
  await expect(page.locator(".register-count")).toContainText("1 selected");

  // It does not cover the end of the list: scrolled right to the bottom, the
  // last row sits above the dock rather than behind it. Every row first --
  // the register hands them to the browser in steps as a sentinel scrolls
  // into reach, so "the end" moves while scrolling to it.
  const scroller = page.locator(".register-card .table-scroll");
  const more = scroller.locator(".more-rows");
  while (await more.count()) await more.click();
  // Scrolled until it stays scrolled: what is left is measured before each
  // step, so this passes only once a step found nothing more below -- rows
  // are still being laid out for a moment after the last lot arrives. Both
  // scrollers, because which one moves depends on the width.
  await expect
    .poll(() =>
      page.evaluate(() => {
        let left = 0;
        for (const one of [
          document.querySelector(".register-card .table-scroll")!,
          document.querySelector("main.content")!,
        ]) {
          left += one.scrollHeight - one.scrollTop - one.clientHeight;
          one.scrollTop = one.scrollHeight;
        }
        return left;
      }),
    )
    .toBeLessThanOrEqual(1);
  const lastRow = scroller.locator("tbody tr").last();
  await expect(lastRow).toBeInViewport();
  const lastBox = (await lastRow.boundingBox())!;
  const dockNow = (await dock.boundingBox())!;
  expect(Math.round(lastBox.y + lastBox.height)).toBeLessThanOrEqual(Math.round(dockNow.y) + 1);

  // Clearing the selection takes it away.
  await banner.getByRole("button", { name: "Clear selection" }).click();
  await expect(dock).toHaveCount(0);
});

test("every screen's table becomes cards, not just the register", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "this is the phone layout");

  for (const screen of [
    "Accounts",
    "Categories",
    "Payee Categorisation",
    "Payee Naming Rules",
    "History",
  ]) {
    await go(page, screen);
    const table = page.locator(".table-scroll table").first();
    await expect(table).toBeVisible();

    const shape = await table.evaluate((node) => {
      const row = node.querySelector("tbody tr");
      return {
        rowDisplay: row ? getComputedStyle(row).display : null,
        headVisible: getComputedStyle(node.querySelector("thead")!).display !== "none",
        hasPrimary: !!node.querySelector("td[data-primary]"),
        overflows: node.scrollWidth > node.clientWidth + 1,
      };
    });

    // The same three properties on every screen: a card per row, the column
    // headings gone because each cell carries its own label, something marked
    // as what the row is about, and nothing running off the side.
    expect(shape.rowDisplay, screen).toBe("grid");
    expect(shape.headVisible, screen).toBe(false);
    expect(shape.hasPrimary, screen).toBe(true);
    expect(shape.overflows, screen).toBe(false);
  }
});

/**
 * The menu, as four sections.
 *
 * These assert the shape rather than a screenshot of it: which words are
 * headers, which of them open a page, and that the one that does not is
 * "Register" -- because the thing under it is the register itself.
 */
test("the menu is four sections, and three of the headers open a page", async ({ page }) => {
  const menu = page.getByRole("button", { name: /Open the menu/ });
  if (await menu.isVisible()) await menu.click();

  const nav = page.locator("nav.side");
  // Register, Reports, the household's own name, Admin.
  await expect(nav.locator(".nav-head")).toHaveCount(4);

  // "Register" heads the section and opens nothing: its page is the first
  // item under it.
  const register = nav.locator(".nav-head", { hasText: /^Register$/ });
  await expect(register).toHaveClass(/nav-head-plain/);

  // The four items under it, in order, including the one that leaves the SPA.
  for (const label of ["Transactions", "Import", "Receipts"]) {
    await expect(nav.getByRole("button", { name: label, exact: true })).toBeVisible();
  }
  await expect(nav.getByRole("link", { name: "Snap a Receipt" })).toBeVisible();

  // And the old entries are gone rather than sitting beside the new ones.
  await expect(nav.getByRole("button", { name: "Household settings" })).toHaveCount(0);
  await expect(nav.getByRole("button", { name: "Payee rules", exact: true })).toHaveCount(0);
});

/**
 * #185: the household's section in the issue's order, with "Payee" a heading
 * of its own over the three payee pages. At both widths, because on a phone
 * the same list is the drawer.
 */
test("the household section lists its pages in order, payees under a heading", async ({
  page,
}) => {
  const menu = page.getByRole("button", { name: /Open the menu/ });
  if (await menu.isVisible()) await menu.click();

  const section = page.locator("nav.side .nav-section").nth(2);
  // `textContent` for the same reason as the household-page test below: what
  // the DOM says, not what CSS renders.
  const entries = await section
    .locator(".nav-child, .nav-subhead")
    .evaluateAll((nodes) =>
      nodes.map((node) => `${node.tagName === "BUTTON" ? "" : "# "}${node.textContent?.trim()}`),
    );
  expect(entries).toEqual([
    "Accounts",
    "Transfers",
    "Categories",
    "# Payee",
    "Payee Merge",
    "Payee Categorisation",
    "Payee Naming Rules",
  ]);

  // The heading is not a control, and it is drawn.
  const heading = section.locator(".nav-subhead", { hasText: /^Payee$/ });
  await expect(heading).toBeVisible();
  await expect(section.getByRole("button", { name: "Payee", exact: true })).toHaveCount(0);

  // The three pages sit further in than the heading, and the heading further
  // in than Categories: the indent is what says what belongs to what.
  const left = async (locator: ReturnType<typeof page.locator>) => {
    const box = (await locator.boundingBox())!;
    const pad = await locator.evaluate((node) => parseFloat(getComputedStyle(node).paddingLeft));
    return box.x + pad;
  };
  const categories = await left(section.getByRole("button", { name: "Categories", exact: true }));
  const head = await left(heading);
  const merge = await left(section.getByRole("button", { name: "Payee Merge", exact: true }));
  expect(head).toBeGreaterThanOrEqual(categories);
  expect(merge).toBeGreaterThan(head);

  // And it still goes where the old "Payee" entry went.
  await section.getByRole("button", { name: "Payee Merge", exact: true }).click();
  await expect(page.locator("main").getByRole("heading", { name: "Payees", level: 1 })).toBeVisible();
});

/**
 * #65: the payee table and the rules were one screen, and on a real ledger the
 * hundreds of payees buried the dozen rules. Each is its own screen now, and
 * neither carries the other's table.
 */
test("payee naming rules and payee categorisation are separate screens", async ({ page }) => {
  await go(page, "Payee Naming Rules");
  const main = page.locator("main");
  await expect(main.getByRole("heading", { name: "Payee naming rules", level: 1 })).toBeVisible();
  await expect(main.getByRole("button", { name: "Add a rule" })).toBeVisible();
  await expect(main.getByRole("searchbox", { name: "Search payees" })).toHaveCount(0);

  await go(page, "Payee Categorisation");
  await expect(main.getByRole("heading", { name: "Payee categorisation", level: 1 })).toBeVisible();
  const search = main.getByRole("searchbox", { name: "Search payees" });
  await expect(search).toBeVisible();
  await expect(main.getByRole("button", { name: "Add a rule" })).toHaveCount(0);

  // The search still narrows the table it sits on.
  const rows = main.locator(".table-scroll tbody tr");
  const before = await rows.count();
  expect(before).toBeGreaterThan(1);
  const first = (await rows.first().locator("td").first().innerText()).trim();
  await search.fill(first);
  await expect(rows.first().locator("td").first()).toHaveText(first);
  expect(await rows.count()).toBeLessThan(before);
});

test("the Reports header opens an index, and the index opens the report", async ({ page }) => {
  await go(page, "Reports");
  await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible();

  // The index names what each report answers, rather than being a row of tabs
  // above one of them.
  // Scoped to the page: the nav has an entry of the same name, by design.
  const card = page.locator("main").getByRole("button", { name: /Income vs Expense/ });
  await expect(card).toBeVisible();
  await card.click();

  await expect(page.getByRole("heading", { name: "Income vs Expense" })).toBeVisible();
  // And the way back, for somebody who arrived here from the nav and has no
  // browser history to press.
  await page.locator(".crumbs").getByRole("button", { name: "Reports" }).click();
  await expect(page.locator("main").getByRole("button", { name: /Income vs Expense/ })).toBeVisible();
});

test("the report is also one click from the menu", async ({ page }) => {
  await go(page, "Income vs Expense");
  await expect(page.getByRole("heading", { name: "Income vs Expense" })).toBeVisible();

  // The nav says where you are, on the child rather than on its header.
  const menu = page.getByRole("button", { name: /Open the menu/ });
  if (await menu.isVisible()) await menu.click();
  await expect(
    page.locator("nav.side").getByRole("button", { name: "Income vs Expense", exact: true }),
  ).toHaveAttribute("aria-current", "page");
});

test("the household page holds the settings and counts what is in the ledger", async ({ page }) => {
  // The header is the household's own name, so the test has to read it rather
  // than know it.
  const menu = page.getByRole("button", { name: /Open the menu/ });
  if (await menu.isVisible()) await menu.click();
  const heads = page.locator("nav.side .nav-head");
  // `textContent`, not `innerText`: the nav headers are uppercased in CSS and
  // `innerText` reports what is *rendered*, so this read "DEMO HOUSEHOLD" and
  // then looked for a heading nothing calls it. The ledger's real name is in
  // the DOM underneath.
  const name = (await heads.nth(2).textContent())?.trim() ?? "";

  await heads.nth(2).click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();

  // Everything the settings panel held is here, on the page.
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.getByLabel("Main currency")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Colour" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Who is in it" })).toBeVisible();

  // And the counts, which a panel never had. A seeded household has accounts
  // and transactions, so these are real numbers rather than zeroes.
  const accounts = page.locator(".stat", { hasText: "Accounts" }).locator(".stat-figure");
  await expect(accounts).toBeVisible();
  expect(Number((await accounts.innerText()).replace(/\D/g, ""))).toBeGreaterThan(0);

  // The rule the whole endpoint is written around: counts, never a total.
  await expect(page.getByText("no such thing as a household total")).toBeVisible();
});

test("payees have a screen of their own, sorted at its headings", async ({
  page,
}, testInfo) => {
  // Desktop only, and the reason is a real gap rather than a quirk of this
  // screen. At 768px and below, `styles.css` renders every `.table-scroll`
  // table as cards and hides `thead` outright -- so the column headings that
  // carry the sort do not exist on a phone, for this list or any other. The
  // house rule that "every list sorts at its column headers" quietly does not
  // hold at that width, app-wide. Asserting it here would only make this one
  // screen carry the failure.
  test.skip(testInfo.project.name !== "desktop", "sorting lives in headings, hidden at phone width");
  await go(page, "Payee Merge");
  await expect(page.getByRole("heading", { name: "Payees" })).toBeVisible();

  // The generic heading, the same one every other list in the app uses: the
  // arrow is only on the column in use, and clicking it turns it around.
  const heading = page.locator("th.sortable", { hasText: "Name" }).first();
  await expect(heading).toHaveAttribute("aria-sort", "ascending");
  await heading.getByRole("button").click();
  await expect(heading).toHaveAttribute("aria-sort", "descending");
});

/**
 * A backup, carried away and then removed (#133).
 *
 * The zip is opened rather than trusted: it has to hold the ledger, its
 * manifest and the README, and no key unless the box was ticked. Then delete,
 * through its confirmation, and the row is gone. At both widths, because the
 * row's three actions are the widest thing in that table on a phone.
 */
test("a backup downloads as a zip that explains itself, and deletes after asking", async ({
  page,
}, testInfo) => {
  const { readFileSync } = await import("node:fs");
  const { inflateRawSync } = await import("node:zlib");

  await go(page, "Application management");
  await page.getByRole("button", { name: "Back up now" }).click();
  const row = page.locator("tbody tr", { hasText: /spendtracker-.*\.sqlite3/ }).first();
  await expect(row).toBeVisible();
  const name = (await row.locator("td[data-primary]").textContent())!.trim();

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    row.getByRole("link", { name: "Download" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe(name.replace(/\.sqlite3$/, ".zip"));
  const zip = readFileSync((await download.path())!);

  // The central directory's names, read without a zip library: each entry's
  // header is "PK\x01\x02" with the name at a fixed offset.
  const names: string[] = [];
  for (let at = zip.indexOf("PK\x01\x02"); at !== -1; at = zip.indexOf("PK\x01\x02", at + 4)) {
    const length = zip.readUInt16LE(at + 28);
    names.push(zip.subarray(at + 46, at + 46 + length).toString("utf8"));
  }
  const folder = name.replace(/\.sqlite3$/, "");
  expect(names.sort()).toEqual([
    `${folder}/README.txt`,
    `${folder}/manifest.json`,
    `${folder}/spendtracker.sqlite3`,
  ]);
  // And the README says what it is, deflated or stored.
  const local = zip.indexOf(`${folder}/README.txt`);
  const header = zip.lastIndexOf("PK\x03\x04", local);
  const method = zip.readUInt16LE(header + 8);
  const size = zip.readUInt32LE(header + 18);
  const start = header + 30 + zip.readUInt16LE(header + 26) + zip.readUInt16LE(header + 28);
  const body = zip.subarray(start, start + size);
  const readme = (method === 8 ? inflateRawSync(body) : body).toString("utf8");
  expect(readme).toContain("HOW TO RESTORE IT");
  expect(readme).toContain("secret.key is NOT in this folder");

  // The save routine opens beside the list and always has the plain download.
  await row.getByRole("button", { name: "Save to Drive/Dropbox…" }).click();
  const panel = page.getByRole("dialog", { name: "Save to Google Drive or Dropbox" });
  await expect(panel.getByRole("link", { name: "Open Google Drive" })).toBeVisible();
  await panel.getByRole("button", { name: "Close" }).click();

  if (testInfo.project.name === "mobile") {
    const overflows = await page
      .locator(".table-scroll table", { hasText: name })
      .evaluate((node) => node.scrollWidth > node.clientWidth + 1);
    expect(overflows).toBe(false);
  }

  await row.getByRole("button", { name: "Delete" }).click();
  const dialog = page.getByRole("dialog", { name: "Delete this backup?" });
  await expect(dialog).toContainText(name);
  await dialog.getByRole("button", { name: "Yes, delete it" }).click();
  await expect(page.locator("tbody tr", { hasText: name })).toHaveCount(0);
});

test("a purchase flagged as a work expense and linked to its repayment turns its W green", async ({
  page,
}) => {
  // What the report says before, so the test holds whatever the seed flagged.
  await go(page, "Reimbursements");
  const recovered = page.locator(".reimb-figure").filter({ hasText: "Recovered" });
  await expect(recovered).toBeVisible();
  // A line per ticked currency (#142): count every line, whichever is ticked.
  const repaid = async () =>
    [...(await recovered.innerText()).matchAll(/(\d+) expenses? repaid/g)].reduce(
      (sum, one) => sum + Number(one[1]),
      0,
    );
  const before = await repaid();
  await go(page, "Transactions");

  // Money out in euros that is not a transfer leg and not already a work
  // expense -- the seed flags a few of its own, in both currencies.
  const purchase = page
    .locator("tbody tr")
    .filter({ has: page.locator("td.amount.neg:not(:empty)") })
    .filter({ hasNot: page.locator(".tag.source-T") })
    .filter({ hasNot: page.locator(".tag.work-owed, .tag.work-paid, .tag.work-off") })
    .filter({ has: page.locator('td[data-label="Account"]', { hasText: /Santander|Visa/ }) })
    .first();
  await purchase.locator("td button").first().click();
  const panel = page.locator(".panel");
  await panel.getByRole("button", { name: "Done" }).waitFor();

  await panel.getByLabel("Reimbursement").selectOption("expected");
  await expect(panel.getByText(/Not reimbursed yet · \d+ days?/)).toBeVisible();

  await panel.getByRole("button", { name: "Find the payment…" }).click();
  const picker = page.getByRole("dialog", { name: "Which payment repaid this?" });
  await picker.getByRole("button", { name: "Link" }).first().click();
  await expect(picker).toHaveCount(0);
  await expect(panel.getByText(/Reimbursed by/)).toBeVisible();
  await panel.getByRole("button", { name: "Done" }).click();

  // The filter that brings the payment along, grouped under it, and the pill
  // is the repaid one: --positive, with the word in its accessible name.
  const filters = page.getByRole("button", { name: /^(Filters|Hide filters)/ });
  if (await filters.isVisible()) await filters.click();
  await page.getByLabel("Work expenses").selectOption("paid");
  await expect(page.locator(".tag.work-paid").first()).toHaveAttribute(
    "aria-label",
    "Work expense — reimbursed",
  );
  await expect(page.locator(".work-child").first()).toBeVisible();

  // And the report counts it as recovered.
  await go(page, "Reimbursements");
  await expect(page.getByRole("heading", { name: "Reimbursements" })).toBeVisible();
  // At least one more: the desktop and phone runs share one ledger, so the
  // other width's link may land in the same count.
  await expect
    .poll(repaid)
    .toBeGreaterThan(before);
});

/**
 * Accounts from a file (#146): the template, a preview, and the import.
 *
 * The template is opened rather than trusted -- its header is the contract
 * the reader holds a file to. Both widths share one seeded ledger, so the
 * names carry the project's and each width takes a different IBAN: an
 * account name or an IBAN can only be used once in a household.
 */
test("accounts import from a file, after a preview, from the template's columns", async ({
  page,
}, testInfo) => {
  const { readFileSync } = await import("node:fs");
  const width = testInfo.project.name;
  const iban = width === "desktop" ? "ES9121000418450200051332" : "GB82WEST12345698765432";

  await go(page, "Accounts");
  await page.getByRole("button", { name: "Import from a file" }).click();
  const panel = page.getByRole("dialog", { name: "Import accounts from a file" });

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    panel.getByRole("link", { name: "Download the template" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe("household-spend-tracker-accounts-template.csv");
  const template = readFileSync((await download.path())!, "utf8").replace(/^﻿/, "");
  expect(template.trim()).toBe(
    "name,type,currency,institution,country,opening_balance,opening_date,iban,note",
  );

  const current = `Imported current ${width}`;
  const savings = `Imported savings ${width}`;
  const csv = [
    template.trim(),
    `${current},checking,EUR,Example Bank,ES,1234.56,2026-01-31,${iban},`,
    `${savings},savings,GBP,,GB,,,,kept apart`,
  ].join("\r\n");
  await panel.locator('input[type="file"]').setInputFiles({
    name: "accounts.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(csv, "utf8"),
  });

  // The server's reading of the file, before anything is kept.
  const preview = panel.locator("tbody tr");
  await expect(preview).toHaveCount(2);
  await expect(preview.filter({ hasText: current })).toContainText("1,234.56");
  await expect(preview.filter({ hasText: current })).toContainText(iban);
  await expect(preview.filter({ hasText: savings })).toContainText("GBP");
  await expect(panel.getByText("2 accounts ready to import")).toBeVisible();

  await panel.getByRole("button", { name: "Import 2 accounts" }).click();
  await expect(panel).toHaveCount(0);

  const table = page.locator(".card table").first();
  await expect(table.locator("tbody tr", { hasText: current })).toContainText("1,234.56");
  await expect(table.locator("tbody tr", { hasText: savings })).toContainText("GBP");
});

/**
 * The New account panel asks for the bank (#12), rather than leaving it to be
 * filled in by opening the account again afterwards.
 */
test("a new account is made with its bank, and the list shows it", async ({ page }, testInfo) => {
  const width = testInfo.project.name;
  const name = `Made with a bank ${width}`;
  const bank = width === "desktop" ? "Example Bank" : "Harbour Savings";

  await go(page, "Accounts");
  await page.getByRole("button", { name: "Add an account" }).click();
  const panel = page.getByRole("dialog", { name: "New account" });
  await panel.getByLabel("Name", { exact: true }).fill(name);
  await panel.getByLabel("Bank or institution", { exact: true }).fill(bank);
  await panel.getByLabel("Note", { exact: true }).fill("Opened for the test");
  await panel.getByRole("button", { name: "Create" }).click();
  await expect(panel).toHaveCount(0);

  const table = page.locator(".card table").first();
  await expect(table.locator("tbody tr", { hasText: name })).toContainText(bank);
});

test("a YNAB export comes in through the household page's one-time import, as one History entry", async ({
  page,
}, testInfo) => {
  // Both widths run against one ledger, so each gets accounts of its own; the
  // second run also meets the first one's repeat-run warning, which is fine.
  const width = testInfo.project.name;
  const current = `YNAB current ${width}`;
  const card = `YNAB card ${width}`;
  // Synthetic, in YNAB's Register shape: a Ready to Assign inflow, a flagged
  // purchase, and one transfer seen from both sides.
  const csv = [
    `"Account","Flag","Date","Payee","Category Group/Category","Category Group","Category","Memo","Outflow","Inflow","Cleared"`,
    `"${current}","","2026-01-05","Test Employer","Inflow: Ready to Assign","Inflow","Ready to Assign","",£0.00,£1000.00,"Cleared"`,
    `"${current}","Orange","2026-01-06","Test Grocer","Everyday: Groceries","Everyday","Groceries","weekly",£25.50,£0.00,"Uncleared"`,
    `"${current}","","2026-01-07","Transfer : ${card}","","","","",£100.00,£0.00,"Reconciled"`,
    `"${card}","","2026-01-07","Transfer : ${current}","","","","",£0.00,£100.00,"Reconciled"`,
  ].join("\r\n");

  // The household page is the third menu header, named after the household.
  const menu = page.getByRole("button", { name: /Open the menu/ });
  if (await menu.isVisible()) await menu.click();
  await page.locator("nav.side .nav-head").nth(2).click();

  await page.getByRole("button", { name: "Start a one-time import" }).click();
  const panel = page.getByRole("dialog", { name: "One-time Import" });
  const next = (name = "Next") => panel.getByRole("button", { name, exact: true });

  await next().click(); // YNAB
  await panel.locator('input[type="file"]').setInputFiles({
    name: "Test Plan as of 2026-01-31 Register.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(csv, "utf8"),
  });
  await next().click();

  // What the server found in it.
  await expect(panel.getByText("Cleared states")).toBeVisible();
  await expect(panel.getByText("1 reconciled", { exact: false })).toHaveCount(0);
  await expect(panel.getByText("2 reconciled", { exact: false })).toBeVisible();
  await expect(next()).toBeEnabled();
  await next().click();

  // The widest step: long option lines in a table, and still no sideways scroll.
  await expect(panel.getByRole("combobox", { name: `Target for ${current}` })).toBeVisible();
  const overflow = await panel.evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);

  // Both accounts are new ones.
  await panel.getByRole("combobox", { name: `Target for ${current}` }).selectOption("create");
  await panel.getByRole("combobox", { name: `Target for ${card}` }).selectOption("create");
  await next().click();

  // Ready to Assign is YNAB's bucket and stays uncategorised.
  await expect(
    panel.getByRole("combobox", { name: "Target for Inflow: Ready to Assign" }),
  ).toBeDisabled();
  await next().click();

  await expect(next("Preview")).toBeDisabled();
  await panel.getByRole("checkbox", { name: /I understand YNAB's/ }).check();
  await next("Preview").click();
  await expect(panel.getByText("Nothing has been imported yet", { exact: false })).toBeVisible();
  await next("Import").click();

  const count = (label: string) =>
    panel.locator(".ynab-counts dt", { hasText: new RegExp(`^${label}$`) }).locator("+ dd");
  await expect(count("Imported")).toHaveText("4");
  await expect(count("Transfers linked")).toHaveText("1");
  await expect(count("Failed")).toHaveText("0");
  await expect(panel.getByText("fix its opening balance", { exact: false })).toBeVisible();
  await expect(panel.locator(".banner.warn li", { hasText: current })).toBeVisible();
  await expect(panel.locator(".banner.warn li", { hasText: card })).toBeVisible();

  // The report's links open a new tab, and the wizard stays where it was.
  const [tab] = await Promise.all([
    page.context().waitForEvent("page"),
    panel.getByRole("link", { name: "History" }).click(),
  ]);
  await expect(tab.getByText("One-time Import · YNAB (CSV)").first()).toBeVisible();
  await tab.close();
  await expect(count("Imported")).toHaveText("4");

  await next("Done").click();
  await expect(panel).toHaveCount(0);

  await go(page, "History");
  await expect(page.getByText("One-time Import · YNAB (CSV)").first()).toBeVisible();
});
