// @vitest-environment jsdom

/**
 * The first-run wizard (#458): which steps each edition walks, the rail's
 * jumps, when Continue may be pressed, the language switching the words at
 * once, the next steps on the finish screen, and what `/setup/complete` is
 * finally sent. Every request goes through a mocked `api`, and each test
 * asserts what was sent or what the screen now says.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
  setUnauthorizedHandler: vi.fn(),
}));

import { api } from "../lib/api";
import { EVERYTHING, setCapabilities, type Capabilities } from "../lib/capabilities";
import { activate } from "../lib/i18n";
import type { User } from "../lib/types";
import { Setup, type SetupState } from "./Setup";
import {
  canJump,
  categoryChoice,
  categoryCount,
  CHAPTER_OF,
  countryFor,
  nextSteps,
  presetIds,
  presetOf,
  type Starter,
  stepsFor,
  toggle,
  weekAround,
  writeDate,
} from "./setup/steps";

const STARTER: Starter = {
  tree: [
    ["seed.group.income", ["seed.category.salary", "seed.category.other_income", "seed.category.reimbursements"]],
    ["seed.group.bills", ["seed.category.rent_mortgage", "seed.category.electricity", "seed.category.water"]],
  ],
  essentials: ["seed.group.income", "seed.category.salary", "seed.category.reimbursements", "seed.group.bills", "seed.category.electricity"],
  required: ["seed.group.income", "seed.category.reimbursements"],
};

const STATE: SetupState = {
  setup_required: true,
  token_needed: true,
  token_path: "/data/setup-token",
  countries: [
    { code: "SE", name: "Sweden", currency: "SEK", date_format: "YYYY-MM-DD", week_start: "monday" },
    { code: "US", name: "United States of America", currency: "USD", date_format: "MM/DD/YYYY", week_start: "sunday" },
  ],
  currencies: ["EUR", "SEK", "USD"],
  palettes: [
    { key: "moss", label: "Moss", light: "#15705c", dark: "#5ac3a6" },
    { key: "clay", label: "Clay", light: "#a0522d", dark: "#e09a74" },
  ],
  next_palette: "moss",
  starter: STARTER,
  install: { edition: "web", data_dir: "/var/lib/spend-tracker", version: "0.17.0", commit: "abc1234" },
};

const DESKTOP: Capabilities = { ...EVERYTHING, second_factor: false, invitations: false, passkeys: false };
const ALEX = { id: "u-alex", email: "alex@example.test", display_name: "Alex", role: "owner" } as User;
const STARTED = { blob: "b1", otpauth_uri: "otpauth://totp/x?secret=ABC", secret: "ABC", recovery_codes: ["c0ffee1234", "deadbeef12"] };

function wrap(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

function answer(state: SetupState) {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path === "/setup/state") return state;
    // The passkey state: not here, so the second step offers the authenticator.
    return { available: false, reason: "not_configured" };
  });
}

const cont = () => fireEvent.click(screen.getByRole("button", { name: "Continue" }));
const continueEnabled = () => !(screen.getByRole("button", { name: "Continue" }) as HTMLButtonElement).disabled;
const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

beforeEach(() => {
  window.localStorage.clear();
  vi.mocked(api.post).mockImplementation(async (path: string) => {
    if (path === "/setup/begin") return STARTED;
    if (path === "/setup/enrol") return { blob: "b2" };
    if (path === "/setup/complete") return ALEX;
    throw new Error(`unexpected ${path}`);
  });
});

afterEach(async () => {
  cleanup();
  setCapabilities(null);
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  window.history.replaceState(null, "", "/");
  await activate("en");
});

// --------------------------------------------------------------------------- //
// The shape
// --------------------------------------------------------------------------- //

describe("the steps of each edition", () => {
  it("web 11, Mac 10 with Touch ID and 9 without, Windows 9", () => {
    expect(stepsFor("web", false)).toEqual([
      "welcome", "overview", "token", "you", "auth", "recovery", "household", "region", "categories", "account", "finish",
    ]);
    expect(stepsFor("mac", true)).toEqual([
      "welcome", "overview", "you", "touch", "recovery", "household", "region", "categories", "account", "finish",
    ]);
    expect(stepsFor("mac", false)).toHaveLength(9);
    expect(stepsFor("windows", true)).toEqual(stepsFor("mac", false));
    expect(stepsFor("windows", false)).not.toContain("touch");
  });

  it("four chapters, in order: welcome, security, the household, review", () => {
    expect(stepsFor("web", false).map((one) => CHAPTER_OF[one])).toEqual([
      "start", "start", "security", "security", "security", "security", "household", "household", "household", "household", "end",
    ]);
  });

  it("the rail jumps back to anything reached, never past the furthest", () => {
    const steps = stepsFor("web", false);
    expect(canJump(steps, "welcome", 3)).toBe(true);
    expect(canJump(steps, "you", 3)).toBe(true);
    expect(canJump(steps, "auth", 3)).toBe(false);
    expect(canJump(steps, "touch", 10)).toBe(false);
  });
});

describe("the finish screen's next steps", () => {
  it("follow the scenario, the edition and whether an account was made", () => {
    expect(nextSteps({ scenario: "solo", invitations: true, account: "made", ynab: false })).toEqual(["import"]);
    expect(nextSteps({ scenario: "solo", invitations: true, account: "skipped", ynab: false })).toEqual(["account", "import"]);
    expect(nextSteps({ scenario: "couple", invitations: true, account: "made", ynab: false })).toEqual(["import", "invite"]);
    expect(nextSteps({ scenario: "family", invitations: false, account: "made", ynab: false })).toEqual(["import"]);
    expect(nextSteps({ scenario: "both", invitations: false, account: "no_currency", ynab: false })).toEqual([
      "account", "import", "shared",
    ]);
    expect(nextSteps({ scenario: "flat", invitations: true, account: "made", ynab: true })).toEqual(["ynab", "import", "invite"]);
  });
});

describe("the starter categories", () => {
  it("presets, a tick that makes the choice custom, and what is sent", () => {
    const all = presetIds(STARTER, "recommended");
    expect(categoryCount(STARTER, all)).toBe(6);
    expect(presetOf(STARTER, all, false)).toBe("recommended");
    expect(categoryCount(STARTER, presetIds(STARTER, "essentials"))).toBe(3);
    expect(categoryCount(STARTER, presetIds(STARTER, "empty"))).toBe(1);

    const less = toggle(STARTER, all, "seed.category.water");
    expect(presetOf(STARTER, less, false)).toBe("custom");
    expect(categoryChoice(STARTER, less)).toEqual({ categories: "recommended", omit: ["seed.category.water"] });

    // The locked ones do not move, alone or with their group.
    expect(toggle(STARTER, all, "seed.category.reimbursements")).toEqual(all);
    expect(toggle(STARTER, all, "seed.group.income")).toEqual(all);
    // A category ticked back brings its group.
    const noBills = toggle(STARTER, all, "seed.group.bills");
    expect(toggle(STARTER, noBills, "seed.category.water").has("seed.group.bills")).toBe(true);
  });
});

describe("formats", () => {
  it("writes a date three ways, finds the week around a day, and guesses a country", () => {
    expect(writeDate("2026-11-09", "DD/MM/YYYY")).toBe("09/11/2026");
    expect(writeDate("2026-11-09", "MM/DD/YYYY")).toBe("11/09/2026");
    const thursday = new Date(Date.UTC(2026, 10, 12, 12));
    expect(weekAround(thursday, "monday").map((d) => d.getUTCDate())).toEqual([9, 15]);
    expect(weekAround(thursday, "sunday").map((d) => d.getUTCDate())).toEqual([8, 14]);
    expect(weekAround(thursday, "saturday").map((d) => d.getUTCDate())).toEqual([7, 13]);
    expect(countryFor("sv-SE", ["en-GB"])).toBe("SE");
    expect(countryFor("en", ["en-GB", "sv"])).toBe("GB");
    expect(countryFor("en", [])).toBe("US");
  });
});

// --------------------------------------------------------------------------- //
// On screen
// --------------------------------------------------------------------------- //

describe("the wizard on a server", () => {
  it("opens on the language, re-words itself when one is chosen, and the rail goes back but not ahead", async () => {
    setCapabilities(EVERYTHING);
    answer(STATE);
    wrap(<Setup onDone={vi.fn()} />);

    await screen.findByRole("heading", { name: "Let's set up your household" });
    // Named in itself, without the country; the tag in small print.
    expect(screen.getByLabelText(/Svenska/)).toBeTruthy();
    expect(screen.getByText("sv-SE")).toBeTruthy();
    const rail = screen.getByRole("navigation", { name: "Setup steps" });
    expect((within(rail).getByRole("button", { name: /Overview/ }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByLabelText(/Svenska/));
    await screen.findByRole("heading", { name: "Nu ställer vi in ditt hushåll" });
    expect(screen.getByRole("button", { name: "Fortsätt" })).toBeTruthy();
    expect(window.localStorage.getItem("spendtracker.locale")).toBe("sv-SE");

    fireEvent.click(screen.getByLabelText(/English/));
    await screen.findByRole("heading", { name: "Let's set up your household" });
    cont();
    await screen.findByRole("heading", { name: "Transactions are the centre of everything" });
    cont();
    await screen.findByRole("heading", { name: "Paste the setup token" });
    // Reached: welcome and overview. Not reached: the token's own successors.
    expect((within(rail).getByRole("button", { name: /You & password/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(rail).getByRole("button", { name: /Welcome/ }));
    await screen.findByRole("heading", { name: "Let's set up your household" });
  });

  it("holds Continue until each step is answered, and walks token, you, a code and the codes", async () => {
    setCapabilities(EVERYTHING);
    answer(STATE);
    wrap(<Setup onDone={vi.fn()} />);
    await screen.findByRole("heading", { name: "Let's set up your household" });
    cont();
    cont();

    await screen.findByRole("heading", { name: "Paste the setup token" });
    expect(screen.getByText(/docker compose exec app python -c/).textContent).toContain("/data/setup-token");
    expect(continueEnabled()).toBe(false);
    type("Setup token", "tok");
    expect(continueEnabled()).toBe(true);
    cont();

    await screen.findByRole("heading", { name: "Create your password" });
    expect(screen.getByLabelText("Email").getAttribute("autocomplete")).toBe("username");
    expect(screen.getByLabelText("Password").getAttribute("autocomplete")).toBe("new-password");
    type("Your name", "Alex");
    type("Email", "alex@example.test");
    expect(continueEnabled()).toBe(false);
    type("Password", "short");
    // The length rule is the server's: Continue asks it.
    expect(continueEnabled()).toBe(true);
    expect(screen.getByText(/5 characters/)).toBeTruthy();
    type("Password", "a long enough passphrase");
    cont();

    await screen.findByRole("heading", { name: "Add a second step" });
    expect(screen.queryByText("Use a passkey")).toBeNull();
    expect(continueEnabled()).toBe(false);
    type("The six digits", "123456");
    cont();

    await screen.findByRole("heading", { name: "Keep your recovery code" });
    expect(screen.getByText("c0ffee1234")).toBeTruthy();
    expect(continueEnabled()).toBe(false);
    fireEvent.click(screen.getByLabelText("I have stored this somewhere other than this computer"));
    expect(continueEnabled()).toBe(true);

    expect(vi.mocked(api.post).mock.calls.map(([path, body]) => [path, body])).toEqual([
      ["/setup/begin", { token: "tok", email: "alex@example.test", display_name: "Alex", password: "a long enough passphrase" }],
      ["/setup/enrol", { blob: "b1", code: "123456" }],
    ]);
  });
});

/** From the welcome to the household step, on the desktop. */
async function toHousehold() {
  await screen.findByRole("heading", { name: "Let's set up your household" });
  cont();
  cont();
  await screen.findByRole("heading", { name: "Create your password" });
  expect(screen.queryByLabelText("Setup token")).toBeNull();
  type("Your name", "Alex");
  type("Email", "alex@example.test");
  type("Password", "a long enough passphrase");
  cont();
  await screen.findByRole("heading", { name: "Keep your recovery code" });
  fireEvent.click(screen.getByLabelText("I have stored this somewhere other than this computer"));
  cont();
  await screen.findByRole("heading", { name: "Name your household" });
}

describe("the wizard on the desktop", () => {
  it("makes the household, its categories and a back-dated account in one request", async () => {
    setCapabilities(DESKTOP);
    answer({ ...STATE, token_needed: false, token_path: null, install: { ...STATE.install!, edition: "windows" } });
    const done = vi.fn();
    wrap(<Setup onDone={done} />);
    await toHousehold();

    // The name follows the scenario until it is typed over.
    expect((screen.getByLabelText("Household name") as HTMLInputElement).value).toBe("Alex's money");
    fireEvent.click(screen.getByLabelText(/Me and partner/));
    expect((screen.getByLabelText("Household name") as HTMLInputElement).value).toBe("Our home");
    // No invitations on this computer: the line says so.
    expect(screen.getByText(/On this computer only you sign in/)).toBeTruthy();
    type("Household name", "");
    expect(continueEnabled()).toBe(false);
    type("Household name", "Home");
    fireEvent.click(screen.getByRole("button", { name: /Clay/ }));
    cont();

    await screen.findByRole("heading", { name: "Where do you keep your money?" });
    fireEvent.change(screen.getByLabelText("Country"), { target: { value: "US" } });
    expect((screen.getByLabelText("Base currency") as HTMLSelectElement).value).toBe("USD");
    expect((screen.getByLabelText("Date format") as HTMLSelectElement).value).toBe("MM/DD/YYYY");
    expect((screen.getByLabelText("First day of the week") as HTMLSelectElement).value).toBe("sunday");
    fireEvent.change(screen.getByLabelText("Date format"), { target: { value: "DD/MM/YYYY" } });
    cont();

    await screen.findByRole("heading", { name: "Pick your starting categories" });
    expect(screen.getByText(/The wizard creates these 6 categories for you, in English/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Water"));
    expect(screen.getByText(/these 5 categories/)).toBeTruthy();
    expect((screen.getByLabelText(/^Reimbursements/) as HTMLInputElement).disabled).toBe(true);
    cont();

    await screen.findByRole("heading", { name: "Add your first account" });
    expect(continueEnabled()).toBe(false);
    type("Account name", "Everyday");
    type("Opening balance", "not money");
    expect(continueEnabled()).toBe(false);
    type("Opening balance", "1234.56");
    fireEvent.change(screen.getByLabelText("Currency"), { target: { value: "SEK" } });
    type("Opening date", "2023-01-01");
    cont();

    await screen.findByRole("heading", { name: "Check and create" });
    expect(screen.getByText("Home")).toBeTruthy();
    expect(screen.getByText("5 categories")).toBeTruthy();
    expect(screen.getByText(/Everyday · Current account · SEK · .* · from 01\/01\/2023/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Create my household" }));

    await screen.findByRole("heading", { name: "You're set up" });
    const complete = vi.mocked(api.post).mock.calls.find(([path]) => path === "/setup/complete")![1];
    expect(complete).toEqual({
      blob: "b1",
      codes_saved: true,
      household: {
        name: "Home",
        colour: "clay",
        base_currency: "USD",
        date_format: "DD/MM/YYYY",
        week_start: "sunday",
        locale: "en",
        categories: "recommended",
        omit: ["seed.category.water"],
      },
      account: { name: "Everyday", kind: "current", currency: "SEK", opening_balance: 123456, opening_date: "2023-01-01" },
    });
    // An account was made and nobody can be invited here: the import alone.
    expect(screen.queryByRole("button", { name: "Add your first account" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Invite someone" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Import a bank statement" }));
    expect(done).toHaveBeenCalledWith(ALEX);
    expect(window.location.search).toBe("?open=import");
  });

  it("skipping the account sends none, and the finish screen offers it", async () => {
    setCapabilities(DESKTOP);
    answer({ ...STATE, token_needed: false, token_path: null, install: { ...STATE.install!, edition: "mac" } });
    wrap(<Setup onDone={vi.fn()} />);
    await toHousehold();
    fireEvent.click(screen.getByLabelText(/More than one household/));
    cont();
    await screen.findByRole("heading", { name: "Where do you keep your money?" });
    cont();
    await screen.findByRole("heading", { name: "Pick your starting categories" });
    fireEvent.click(screen.getByRole("button", { name: /Essentials/ }));
    cont();
    await screen.findByRole("heading", { name: "Add your first account" });
    fireEvent.click(screen.getByRole("button", { name: /My currency isn't here/ }));

    await screen.findByRole("heading", { name: "Check and create" });
    expect(screen.getByText("After setup — currency not in the list")).toBeTruthy();
    expect(screen.getByText("3 categories")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Create my household" }));
    await screen.findByRole("heading", { name: "You're set up" });

    const complete = vi.mocked(api.post).mock.calls.find(([path]) => path === "/setup/complete")![1] as Record<string, unknown>;
    expect(complete.account).toBeUndefined();
    expect((complete.household as { omit: string[] }).omit).toEqual(["seed.category.other_income", "seed.category.rent_mortgage", "seed.category.water"]);
    expect(screen.getByRole("button", { name: "Add your first account" })).toBeTruthy();
    expect(screen.getByText("Then the app is set up, and you create the account from Accounts.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Add another household" })).toBeTruthy();
  });
});
