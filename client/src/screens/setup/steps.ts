/**
 * The first-run wizard's shape (#458), apart from how it is drawn: which
 * steps an edition walks, the chapter each belongs to, what the review's next
 * steps are, and which starter categories a choice keeps. Plain functions, so
 * the rules are tested without rendering anything.
 */

/** Every step any edition has, in the order they come. */
export const ALL_STEPS = [
  "welcome",
  "overview",
  "token",
  "you",
  "touch",
  "auth",
  "recovery",
  "household",
  "region",
  "categories",
  "account",
  "finish",
] as const;
export type StepKey = (typeof ALL_STEPS)[number];

export type Chapter = "start" | "security" | "household" | "end";

export const CHAPTER_OF: Record<StepKey, Chapter> = {
  welcome: "start",
  overview: "start",
  token: "security",
  you: "security",
  touch: "security",
  auth: "security",
  recovery: "security",
  household: "household",
  region: "household",
  categories: "household",
  account: "household",
  finish: "end",
};

export const CHAPTERS: readonly Chapter[] = ["start", "security", "household", "end"];

/** Which installation this is: a server, or the desktop app on a Mac or on Windows. */
export type Edition = "web" | "mac" | "windows";

/**
 * The steps of an edition. A server asks for the token and a second step;
 * the desktop asks for neither (#320), and a Mac whose shell can prompt for
 * Touch ID offers it. Web 11, Mac 10 or 9, Windows 9.
 */
export function stepsFor(edition: Edition, touchOffered: boolean): StepKey[] {
  return ALL_STEPS.filter((step) => {
    if (step === "token" || step === "auth") return edition === "web";
    if (step === "touch") return edition === "mac" && touchOffered;
    return true;
  });
}

/**
 * Where the rail may jump: back to any step already reached, never past the
 * furthest one.
 */
export function canJump(steps: readonly StepKey[], to: StepKey, furthest: number): boolean {
  const index = steps.indexOf(to);
  return index >= 0 && index <= furthest;
}

// --------------------------------------------------------------------------- //
// Who the household is for
// --------------------------------------------------------------------------- //

/** Guidance only: it suggests a name and says what to do afterwards, and is stored nowhere. */
export const SCENARIOS = ["solo", "couple", "both", "family", "flat"] as const;
export type Scenario = (typeof SCENARIOS)[number];

/** What the finish screen offers next. */
export type NextStep = "ynab" | "account" | "import" | "shared" | "invite";

/** How the first-account step ended. */
export type AccountOutcome = "made" | "skipped" | "no_currency";

/**
 * The next steps after setup, in order: the first account when none was
 * made, a statement import, then one more that follows the scenario --
 * another household for "more than one", an invitation for the shared ones
 * where invitations exist, nothing for "just me". Bringing categories from
 * YNAB opens that import first.
 */
export function nextSteps({
  scenario,
  invitations,
  account,
  ynab,
}: {
  scenario: Scenario;
  invitations: boolean;
  account: AccountOutcome;
  ynab: boolean;
}): NextStep[] {
  const steps: NextStep[] = [];
  if (ynab) steps.push("ynab");
  if (account !== "made") steps.push("account");
  steps.push("import");
  if (scenario === "both") steps.push("shared");
  else if (scenario !== "solo" && invitations) steps.push("invite");
  return steps;
}

// --------------------------------------------------------------------------- //
// Starter categories
// --------------------------------------------------------------------------- //

/** The starter tree as the server seeds it: group id, then its categories' ids. */
export type Tree = readonly (readonly [string, readonly string[]])[];

export type Preset = "recommended" | "essentials" | "empty" | "ynab";

export interface Starter {
  tree: Tree;
  essentials: readonly string[];
  /** The Income group and Reimbursements: the reports need them, so they stay ticked. */
  required: readonly string[];
}

/** Every id a preset ticks. YNAB keeps only what the reports need. */
export function presetIds(starter: Starter, preset: Preset): Set<string> {
  if (preset === "essentials") return new Set(starter.essentials);
  if (preset === "empty" || preset === "ynab") return new Set(starter.required);
  return new Set(starter.tree.flatMap(([group, ids]) => [group, ...ids]));
}

/** The preset a set of ticks is exactly, or "custom". */
export function presetOf(starter: Starter, ticked: ReadonlySet<string>, ynab: boolean): Preset | "custom" {
  if (ynab) return "ynab";
  for (const preset of ["recommended", "essentials", "empty"] as const) {
    const wanted = presetIds(starter, preset);
    if (wanted.size === ticked.size && [...wanted].every((id) => ticked.has(id))) return preset;
  }
  return "custom";
}

/** Tick or untick one id. A category brings its group with it; a group takes its categories. */
export function toggle(starter: Starter, ticked: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(ticked);
  if (starter.required.includes(id)) return next;
  const group = starter.tree.find(([groupId]) => groupId === id);
  if (group) {
    const on = !next.has(id);
    for (const one of [id, ...group[1]]) {
      if (starter.required.includes(one)) continue;
      if (on) next.add(one);
      else next.delete(one);
    }
    return next;
  }
  if (next.has(id)) next.delete(id);
  else {
    next.add(id);
    const parent = starter.tree.find(([, ids]) => ids.includes(id));
    if (parent) next.add(parent[0]);
  }
  return next;
}

/** How many categories (not groups) a set of ticks makes. */
export function categoryCount(starter: Starter, ticked: ReadonlySet<string>): number {
  return starter.tree.reduce((sum, [, ids]) => sum + ids.filter((id) => ticked.has(id)).length, 0);
}

/**
 * What `/setup/complete` is sent about the categories: always the whole tree
 * as the base, less everything not ticked, so a custom choice and a preset
 * travel the same way.
 */
export function categoryChoice(starter: Starter, ticked: ReadonlySet<string>): {
  categories: "recommended";
  omit: string[];
} {
  const every = starter.tree.flatMap(([group, ids]) => [group, ...ids]);
  return { categories: "recommended", omit: every.filter((id) => !ticked.has(id)) };
}

// --------------------------------------------------------------------------- //
// Formats
// --------------------------------------------------------------------------- //

export const DATE_FORMATS = ["YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY"] as const;
export type DateFormat = (typeof DATE_FORMATS)[number];

/** An ISO date written in one of the household formats. */
export function writeDate(iso: string, format: DateFormat): string {
  const [year, month, day] = iso.split("-");
  if (format === "DD/MM/YYYY") return `${day}/${month}/${year}`;
  if (format === "MM/DD/YYYY") return `${month}/${day}/${year}`;
  return iso;
}

const WEEKDAY: Record<"monday" | "sunday" | "saturday", number> = { sunday: 0, monday: 1, saturday: 6 };

/** The first and last day of the week holding `today`, for a week starting on `start`. */
export function weekAround(today: Date, start: "monday" | "sunday" | "saturday"): [Date, Date] {
  const back = (today.getUTCDay() - WEEKDAY[start] + 7) % 7;
  const first = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() - back, 12));
  const last = new Date(first.getTime() + 6 * 86_400_000);
  return [first, last];
}

/** Where a language's speakers most likely keep their money, for the region step's first guess. */
export function countryFor(locale: string, browser: readonly string[]): string {
  const language = locale.split("-")[0].toLowerCase();
  for (const tag of browser) {
    const [lang, region] = tag.split("-");
    if (lang?.toLowerCase() === language && region && /^[A-Za-z]{2}$/.test(region)) return region.toUpperCase();
  }
  const own = locale.split("-")[1];
  if (own && /^[A-Za-z]{2}$/.test(own)) return own.toUpperCase();
  return language === "en" ? "US" : "";
}
