/**
 * The shell: who you are, which household, which screen.
 *
 * Three states before the app proper -- a fresh instance wants setting up, a
 * signed-out browser wants a sign-in, and a signed-in one with no household
 * wants to make one.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, setUnauthorizedHandler } from "./lib/api";
import { Field, Problem } from "./components/bits";
import { RecoveryModeBanner } from "./components/RecoveryModeBanner";
import { ReenrolmentDue } from "./components/ReenrolmentDue";
import { ScreenBoundary } from "./components/ScreenBoundary";
import { SignInNotice } from "./components/SignInNotice";
import { AcceptInvite } from "./screens/AcceptInvite";
import { ResetAccount } from "./screens/ResetAccount";
import { Accounts } from "./screens/Accounts";
import { Categories } from "./screens/Categories";
import { Admin } from "./screens/Admin";
import { ApplicationManagement } from "./screens/ApplicationManagement";
import { History } from "./screens/History";
import { HouseholdPage } from "./screens/Household";
import { Import } from "./screens/Import";
import { ImportGuide } from "./screens/ImportGuide";
import { Payees } from "./screens/Payees";
import { PayeeCategorisation } from "./screens/PayeeCategorisation";
import { Receipts } from "./screens/Receipts";
import { REPORTS, Report, Reports, reportFor, reportScreen } from "./screens/Reports";
import type { ReportScreen } from "./screens/Reports";
import { Register } from "./screens/Register";
import type { RegisterPreset } from "./screens/Register";
import { Profile } from "./screens/Profile";
import { applyColours } from "./lib/theme";
import { claimScreens, readSticky, writeSticky } from "./lib/sticky";
import { forgetTheBrowser } from "./lib/signedOut";
import { Rules } from "./screens/Rules";
import { Setup } from "./screens/Setup";
import { SignIn } from "./screens/SignIn";
import { Transfers } from "./screens/Transfers";
import type { Household, User } from "./lib/types";
import { plural, t } from "@lingui/core/macro";
import { useLingui } from "@lingui/react";
import { Trans } from "@lingui/react/macro";

type Screen =
  | "register"
  | "accounts"
  | "categories"
  | "receipts"
  | "reports"
  | ReportScreen
  | "household"
  | "payees"
  | "payee-categorisation"
  | "transfers"
  | "import-guide"
  | "import"
  | "rules"
  | "history"
  | "admin"
  | "application";

/**
 * The menu, as four sections.
 *
 * A section header is a heading *and*, for three of the four, a page of its
 * own: Reports opens the index of reports, the household's name opens the
 * household, Admin opens Admin. "Register" is the exception and has no page,
 * because the thing you would want from it is the register itself, which is
 * the first item under it.
 *
 * The shape is data rather than markup so that the two things that must agree
 * -- what the menu offers and what `<main>` will render -- are one list. A
 * screen added to the union with no entry here is unreachable; an entry here
 * with no case below is a dead button.
 */
type NavItem =
  /** A screen in this app. */
  | { key: Screen; label: string; ownerOnly?: boolean }
  /** A page outside it. `/snap` is its own HTML, for a phone at a till. */
  | { href: string; label: string };

type NavChild =
  | NavItem
  /**
   * A second-level heading with its own items indented under it (#185). The
   * heading opens nothing -- it has no page, and a button that goes nowhere
   * is a dead link -- so it is words, not a control.
   */
  | { heading: string; children: NavItem[] };

type NavSection = {
  id: string;
  label: string;
  /** The header's own page, when it has one. */
  screen?: Screen;
  /** Whether the header's *link* is the owner's alone. The section still shows. */
  ownerOnly?: boolean;
  children: NavChild[];
};

/**
 * The browser tab's title (#189): "Spend Tracker - <page> - <household>", so
 * several tabs of the app -- and of two ledgers -- can be told apart. The page
 * is named as the menu names it, one list, so a renamed menu entry renames its
 * tab too. The household's own page is already named after it, so the caller
 * leaves the household out there rather than say it twice.
 */
export const APP_NAME = "Spend Tracker";

export function tabTitle(page?: string | null, household?: string | null): string {
  const parts = [APP_NAME, page, household];
  return parts.filter(Boolean).join(" - ");
}

/** The menu's label for a screen: an item, an item under a heading, or a section's own page. */
export function pageLabel(sections: NavSection[], screen: Screen): string | null {
  for (const section of sections) {
    if (section.screen === screen) return section.label;
    for (const child of section.children) {
      const items = "heading" in child ? child.children : [child];
      for (const item of items) if ("key" in item && item.key === screen) return item.label;
    }
  }
  return null;
}

/** Who else is here: "Robin is here too", "Robin, Sam are here too". */
function presenceText(names: string[]): string {
  const who = names.join(", ");
  return plural(names.length, { one: `${who} is here too`, other: `${who} are here too` });
}

/** Sets the tab's title for the screen on show. */
function useTabTitle(page: string | null, household?: string | null) {
  useEffect(() => {
    document.title = tabTitle(page, household);
  }, [page, household]);
}

export function menu(householdName: string): NavSection[] {
  return [
    {
      id: "register",
      label: t({ message: "Register", comment: "Menu item and the name of its screen. See GLOSSARY.md" }),
      children: [
        { key: "register", label: t({ message: "Transactions", comment: "Menu item and the name of its screen. See GLOSSARY.md" }) },
        { key: "import", label: t({ message: "Import", comment: "Menu item and the name of its screen: noun, one import of a statement. See GLOSSARY.md" }) },
        { key: "receipts", label: t({ message: "Receipts", comment: "Menu item and the name of its screen: noun, photos or PDFs of receipts. See GLOSSARY.md" }) },
        // Reachable since receipts landed, and for a while linked from
        // nowhere: the only way to open it was to know the URL.
        { href: "/snap", label: t`Snap a Receipt` },
      ],
    },
    {
      id: "reports",
      label: t({ message: "Reports", comment: "Menu item and the name of its screen. See GLOSSARY.md" }),
      screen: "reports",
      children: REPORTS.map((one) => ({ key: reportScreen(one.key), label: one.label })),
    },
    {
      id: "household",
      // The household's own name, so two ledgers in one browser are told apart
      // in the menu as well as by the colour.
      label: householdName,
      screen: "household",
      // The order is #185's: the three things a ledger is made of, then the
      // three payee screens gathered under a heading of their own.
      children: [
        { key: "accounts", label: t({ message: "Accounts", comment: "Menu item and the name of its screen: noun, bank or cash accounts. See GLOSSARY.md" }) },
        { key: "transfers", label: t({ message: "Transfers", comment: "Menu item and the name of its screen: noun, money moved between your own accounts. See GLOSSARY.md" }) },
        { key: "categories", label: t({ message: "Categories", comment: "Menu item and the name of its screen. See GLOSSARY.md" }) },
        {
          heading: t({ message: "Payee", comment: "Heading on the app's menu and frame: noun, who was paid or who paid. See GLOSSARY.md" }),
          children: [
            // The list of payees, whose job in practice is folding two
            // spellings of one shop together.
            { key: "payees", label: t({ message: "Payee Merge", comment: "Menu item and the name of its screen. See GLOSSARY.md" }) },
            // Two screens where there was one "Payee Rules" (#65): the naming
            // rules are a dozen rows and the categorisation table is one row
            // per payee, and on one page the second buried the first.
            { key: "payee-categorisation", label: t({ message: "Payee Categorisation", comment: "Menu item and the name of its screen. See GLOSSARY.md" }) },
            { key: "rules", label: t`Payee Naming Rules` },
          ],
        },
      ],
    },
    {
      id: "admin",
      label: t({ message: "Admin", comment: "Menu item and the name of its screen" }),
      screen: "admin",
      // The Admin *screen* is the owner's. History is not -- it is a
      // household's own record of what everyone in it did, and it has been
      // reachable by every member since it landed. So the section is drawn for
      // everybody and only the header stops being a link.
      ownerOnly: true,
      children: [
        { key: "history", label: t({ message: "History", comment: "Menu item and the name of its screen. See GLOSSARY.md" }) },
        // Every member's, like History: it explains what the Import screen
        // does, and nothing behind it is private (#73).
        { key: "import-guide", label: t`How import works` },
        // Owner-only in its own right, and separately from the header: History
        // above it is every member's, and this one is about the installation
        // rather than about a ledger. Hiding it is a courtesy -- every route
        // behind it answers 403 to a member.
        { key: "application", label: t({ message: "Application management", comment: "Menu item and the name of its screen. See GLOSSARY.md" }), ownerOnly: true },
      ],
    },
  ];
}

/**
 * Where to go once this browser is somebody again.
 *
 * `/snap` redirects here when the session has lapsed, which with a 30-day
 * trusted device happens roughly monthly -- but when it happens the person is
 * holding a receipt and a phone, and landing them on the register is a lost
 * receipt.
 *
 * Only a same-origin path, and only one this app actually has. A `next` that
 * can name any URL is an open redirect, and this one arrives in a query
 * string.
 */
const RETURNS_TO = ["/snap"];

function returnTo(): string | null {
  const wanted = new URLSearchParams(window.location.search).get("next");
  return wanted && RETURNS_TO.includes(wanted) ? wanted : null;
}

/**
 * `/?open=<screen>&household=<id>` opens one screen in a fresh tab, for a
 * page that must stay where it is -- the One-time Import's report links to
 * the accounts it made, to History and to the payee rules its bank text
 * feeds, without closing the wizard. Only the screens named here, for the
 * same reason `next` is fenced: it arrives in a query string. Read once, then
 * the address goes back to `/`.
 */
const OPENS_IN_NEW_TAB = ["accounts", "history", "rules"] as const;
type OpensInNewTab = (typeof OPENS_IN_NEW_TAB)[number];

/**
 * What `?open=` asked for. Reads the address and changes nothing: it runs as a
 * `useState` initialiser, which Strict Mode calls twice, and when it also put
 * the address back the second call found `?open=` gone and answered "nothing"
 * (#110). `putTheAddressBack` does that half, from an effect.
 */
export function openedAt(): { screen: OpensInNewTab | null; household: string } {
  const query = new URLSearchParams(window.location.search);
  const wanted = query.get("open");
  const screen = OPENS_IN_NEW_TAB.find((one) => one === wanted) ?? null;
  // An id, not a path: `../me` arrived in a query string and went straight
  // into an API path (#197). The shell still checks it against the member's
  // households before using it; this is the fence at the door.
  const household = screen ? (query.get("household") ?? "") : "";
  return { screen, household: /^[\w-]{1,64}$/.test(household) ? household : "" };
}

/** Once a screen has been opened from `?open=`, the address goes back to `/`. */
export function putTheAddressBack(opened: { screen: OpensInNewTab | null }): void {
  if (opened.screen) window.history.replaceState(null, "", "/");
}

/** `/invite/<token>` is a real URL people are handed, so the shell reads it first. */
function inviteToken(): string | null {
  return tokenAt("invite");
}

/** `/reset/<token>`: an account reset link (#284), read first for the same reason. */
function resetToken(): string | null {
  return tokenAt("reset");
}

function tokenAt(prefix: "invite" | "reset"): string | null {
  const match = window.location.pathname.match(new RegExp(`^/${prefix}/([^/]+)/?$`));
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    // A half-copied link can contain a stray %, and decodeURIComponent throws.
    // This runs in a useState initializer, so an escape here blanks the app;
    // hand the raw text through and let the server say the link is no good.
    return match[1];
  }
}

export function App() {
  useLingui();
  const client = useQueryClient();
  const [user, setUserState] = useState<User | null>(null);
  // Who the cache was last filled for. Not state: it is read and written inside
  // the setter below, before the render that would read the cache.
  const cachedFor = useRef<string | null>(null);

  /**
   * Every change of who is signed in goes through here.
   *
   * The query cache is not keyed by user -- `["households"]` is the same key
   * for everybody -- so whatever it holds belongs to whoever was signed in when
   * it was filled. It is cleared when a session ends underneath us (a 401) and
   * when somebody other than the last user arrives, *before* the state change,
   * because the shell's first render reads the cache synchronously: clearing in
   * an effect afterwards would already have painted the previous person's
   * households. Only an explicit sign-out used to clear it, so an expired
   * session followed by a second person at the same tab showed them the
   * first person's ledger for as long as the cache kept it.
   */
  const setUser = useCallback(
    (next: User | null) => {
      if (next === null || next.id !== cachedFor.current) {
        void client.cancelQueries();
        client.clear();
      }
      cachedFor.current = next?.id ?? null;
      // Before the state change, for the same reason as the cache: the
      // register reads its remembered filters in its first render.
      if (next) claimScreens(next.id);
      setUserState(next);
    },
    [client],
  );
  /** An explicit sign-out: this browser forgets whoever it was (#198). */
  const signedOut = useCallback(() => {
    forgetTheBrowser();
    setUser(null);
  }, [setUser]);
  const [ready, setReady] = useState(false);
  const [setupNeeded, setSetupNeeded] = useState(false);
  const [token, setToken] = useState(inviteToken);
  const [reset, setReset] = useState(resetToken);

  useEffect(() => {
    (async () => {
      try {
        const health = await api.get<{ setup_required: boolean }>("/health");
        setSetupNeeded(health.setup_required);
        if (!health.setup_required) {
          try {
            setUser(await api.get<User>("/me"));
          } catch {
            setUser(null);
          }
        }
      } finally {
        setReady(true);
      }
    })();
  }, []);

  // Registered after the boot probe above, which 401s normally while signed
  // out; from here on a 401 means a session that was good has ended.
  useEffect(() => {
    if (!ready) return;
    setUnauthorizedHandler(() => setUser(null));
    return () => setUnauthorizedHandler(null);
  }, [ready, setUser]);

  // The pages before the shell have no menu entry, so they are named here.
  // Undefined once the shell is up: it names its own screens, and an effect
  // here would run after its and overwrite them.
  const before = !ready
    ? null
    : setupNeeded
      ? t({ message: "Set up", comment: "Menu item and the name of its screen" })
      : reset
        ? t({ message: "Reset", comment: "Menu item and the name of its screen" })
        : token
        ? t({ message: "Invitation", comment: "Menu item and the name of its screen. See GLOSSARY.md" })
        : !user
          ? t({ message: "Sign in", comment: "Menu item and the name of its screen. See GLOSSARY.md" })
          : undefined;
  useEffect(() => {
    if (before !== undefined) document.title = tabTitle(before);
  }, [before]);

  if (!ready)
    return (
      <div className="centred muted">
        <Trans comment="Text on the app's menu and frame">Loading…</Trans>
      </div>
    );
  if (setupNeeded)
    return (
      <Setup
        onDone={(owner) => {
          setSetupNeeded(false);
          setUser(owner);
        }}
      />
    );
  // Before "already signed in": following the link signs nobody in and
  // swaps no session, so whoever this browser is does not matter to it.
  if (reset)
    return (
      <ResetAccount
        token={reset}
        onDone={() => {
          // Spent: forget it with the URL, and land on the sign-in page (or
          // the shell, for whoever this browser already was).
          window.history.replaceState(null, "", "/");
          setReset(null);
        }}
      />
    );
  if (token && user)
    return (
      <AlreadySignedIn
        user={user}
        onSignedOut={signedOut}
        onIgnore={() => {
          window.history.replaceState(null, "", "/");
          setToken(null);
        }}
      />
    );
  if (token)
    return (
      <AcceptInvite
        token={token}
        onDone={(joined) => {
          // Forget the token as well as the URL: it is spent, and leaving it in
          // state lands the new account on "you're already signed in".
          window.history.replaceState(null, "", "/");
          setToken(null);
          setUser(joined);
        }}
      />
    );
  if (!user) return <SignIn onDone={setUser} />;
  // Signed in and asked for somewhere else: go, once, before the shell loads a
  // register nobody wanted.
  const onward = returnTo();
  if (onward) {
    window.location.replace(onward);
    return <div className="centred muted">Taking you back…</div>;
  }
  return <Signedin user={user} onSignedOut={signedOut} />;
}

/**
 * An invitation opened in a browser that is already somebody.
 *
 * Accepting would silently swap the session for a different account, so it asks
 * rather than guessing.
 */
function AlreadySignedIn({
  user,
  onSignedOut,
  onIgnore,
}: {
  user: User;
  onSignedOut: () => void;
  onIgnore: () => void;
}) {
  const signOut = useMutation({
    mutationFn: () => api.del("/session"),
    onSuccess: onSignedOut,
  });

  return (
    <div className="centred">
      <h1>
        <Trans>You're already signed in</Trans>
      </h1>
      <p className="muted small">
        <Trans>
          This browser is signed in as {user.display_name}. An invitation makes a new account, so
          sign out first if the link is meant for somebody else.
        </Trans>
      </p>
      <div className="card">
        <Problem error={signOut.error} />
        <button className="primary" onClick={() => signOut.mutate()}>
          <Trans>Sign out and accept the invitation</Trans>
        </button>
        <p />
        <button onClick={onIgnore}>
          <Trans>Ignore it and carry on</Trans>
        </button>
      </div>
    </div>
  );
}

const NAV_COLLAPSED_KEY = "spendtracker.shell.navCollapsed";

function Signedin({ user, onSignedOut }: { user: User; onSignedOut: () => void }) {
  // The menu and the tab title are built from the active catalog, so the
  // shell re-renders when another one arrives (`lib/i18n.ts`).
  useLingui();
  const client = useQueryClient();
  const [opened] = useState(openedAt);
  useEffect(() => putTheAddressBack(opened), [opened]);
  const [screen, setScreen] = useState<Screen>(opened.screen ?? "register");
  //: Where the register should open when a report sends somebody there: a
  //: Work expenses view, and maybe one row's panel. Held here because the
  //: register is unmounted while a report is on screen, so it cannot be told
  //: afterwards; it reads this once as it mounts.
  const [registerPreset, setRegisterPreset] = useState<RegisterPreset | null>(null);
  const [householdId, setHouseholdId] = useState<string>(opened.household);
  const [navOpen, setNavOpen] = useState(false);
  //: The desktop menu folded down to its rail (#161). Per browser, like the
  //: appearance: how much of the window this screen gives the menu is a fact
  //: about the screen, not the ledger. Opening a page leaves it as it is.
  const [navCollapsed, setNavCollapsedState] = useState(
    () => readSticky(NAV_COLLAPSED_KEY, false) ?? false,
  );
  const setNavCollapsed = (next: boolean) => {
    writeSticky(NAV_COLLAPSED_KEY, next);
    setNavCollapsedState(next);
  };
  const [profileOpen, setProfileOpen] = useState(false);

  const households = useQuery({
    queryKey: ["households"],
    queryFn: () => api.get<Household[]>("/households"),
  });

  // Presence is read off the sessions the server already keeps roughly current,
  // so "someone else is here" costs one small poll and no extra bookkeeping.
  const presence = useQuery({
    queryKey: ["presence"],
    queryFn: () => api.get<{ online: User[] }>("/presence"),
    refetchInterval: 30_000,
  });

  const signOut = useMutation({
    mutationFn: () => api.del("/session"),
    onSuccess: () => {
      client.clear();
      onSignedOut();
    },
  });

  // Resolved against the member's own households before anything uses it:
  // `householdId` can come from a query string (#197).
  const list = households.data ?? [];
  const household = list.find((h) => h.id === householdId) ?? list[0];
  const inboxId = household?.id;

  // The nav badge's count. Its own small query rather than a field on
  // /households, because it changes on a different clock from everything else
  // there and it is one indexed read.
  const inbox = useQuery({
    queryKey: ["receipts", inboxId, "unattached"],
    queryFn: () =>
      api.get<{ id: string }[]>(
        `/households/${encodeURIComponent(inboxId!)}/receipts?unattached=true`,
      ),
    enabled: Boolean(inboxId),
  });

  // The page takes the colour of whichever household is on screen. Switching is
  // the moment it matters: two ledgers in one browser otherwise look identical,
  // which is how a statement lands in the wrong one.
  const colours = household?.colours ?? null;
  useEffect(() => {
    applyColours(colours);
  }, [colours]);

  // Before the early returns below, so the hook runs on every render.
  useTabTitle(
    household
      ? pageLabel(menu(household.name), screen)
      : households.isSuccess && list.length === 0
        ? t({ message: "New household", comment: "Menu item and the name of its screen" })
        : null,
    // Its own page is already its name. Decided by the screen, not by
    // comparing names: a household called "Transfers" keeps its name there.
    screen === "household" ? null : household?.name,
  );

  if (households.isLoading)
    return (
      <div className="centred muted">
        <Trans comment="Text on the app's menu and frame">Loading…</Trans>
      </div>
    );
  // An error is not an empty instance. Without this branch a failed read shows
  // "your household", as if the ledger had never existed.
  if (households.isError)
    return (
      <div className="centred">
        <h1>
          <Trans>Couldn't load your households</Trans>
        </h1>
        <Problem error={households.error} />
        <button className="primary" onClick={() => households.refetch()}>
          <Trans comment="Button on the app's menu and frame">Try again</Trans>
        </button>
      </div>
    );
  if (list.length === 0) return <FirstHousehold onCreated={() => households.refetch()} />;
  if (!household)
    return (
      <div className="centred muted">
        <Trans comment="Text on the app's menu and frame">Loading…</Trans>
      </div>
    );

  const others = (presence.data?.online ?? []).filter((one) => one.id !== user.id);
  const waiting = inbox.data?.length ?? 0;
  const sections = menu(household.name);
  const openReport = reportFor(screen);

  /** Go somewhere, and on a phone get out of the way of where you went. */
  const go = (to: Screen) => {
    // Going anywhere from the menu is a fresh start: a filter a report sent
    // somebody to must not come back the next time they open the register.
    setRegisterPreset(null);
    setScreen(to);
    setNavOpen(false);
  };

  /**
   * One entry in the menu, at whatever depth it sits. `null` for an entry
   * this person may not open, so hiding it is the same rule at every level.
   */
  const navItem = (child: NavItem, className: string) => {
    if ("ownerOnly" in child && child.ownerOnly && user.role !== "owner") return null;
    return "href" in child ? (
      // A new tab, so the ledger underneath is still where it was.
      <a
        className={`nav-link ${className}`}
        href={child.href}
        key={child.href}
        target="_blank"
        rel="noopener"
      >
        {child.label}
      </a>
    ) : (
      <button
        className={className}
        key={child.key}
        aria-current={screen === child.key ? "page" : undefined}
        onClick={() => go(child.key)}
      >
        {child.label}
        {/* Only while there is something waiting. An inbox nobody is
            reminded of is a folder, and a badge that is always there is
            furniture nobody sees -- so it disappears at zero, which is also
            the only moment it is worth noticing. */}
        {child.key === "receipts" && waiting > 0 ? (
          <span className="nav-badge" aria-label={t({ message: `${waiting} waiting`, comment: "Screen-reader name on the app's menu and frame" })}>
            {waiting}
          </span>
        ) : null}
      </button>
    );
  };

  /** The register, opened the way a report asked for it. */
  const openRegister = (preset: RegisterPreset) => {
    setRegisterPreset(preset);
    setScreen("register");
    setNavOpen(false);
  };

  return (
    <div className={navCollapsed ? "shell nav-collapsed" : "shell"}>
      {/* The phone header. Hidden above the breakpoint, so on a desktop this
          is exactly the layout it always was -- the nav is the nav, and none
          of this exists. */}
      <header className="topbar">
        <button
          className="menu-button"
          aria-label={navOpen ? t`Close the menu` : t`Open the menu`}
          aria-expanded={navOpen}
          aria-controls="main-nav"
          onClick={() => setNavOpen(!navOpen)}
        >
          <span aria-hidden="true">{navOpen ? "\u2715" : "\u2630"}</span>
        </button>
        <span className="brand">Spend Tracker</span>
      </header>

      {/* Tapping away closes it. Without this the only way out of the drawer
          is the button it came from, which on a phone is a thing you have to
          be told. */}
      {navOpen && (
        <div className="nav-backdrop" onClick={() => setNavOpen(false)} aria-hidden="true" />
      )}

      <nav className={navOpen ? "side open" : "side"} id="main-nav">
        {/* Only the pages scroll. Who is here and Sign out stay pinned under
            them, so the one way out of the app never scrolls out of view, and
            the list scrolls only when the window is shorter than the menu. */}
        <div className="nav-scroll">
          <div className="brand">Spend Tracker</div>

          {list.length > 1 && (
            <select
              value={household.id}
              onChange={(e) => setHouseholdId(e.target.value)}
              style={{ marginBottom: 10 }}
            >
              {list.map((one) => (
                <option key={one.id} value={one.id}>
                  {one.name}
                </option>
              ))}
            </select>
          )}

          {sections.map((section) => {
            // The header is a link when it has a page and this person may open
            // it. Otherwise it is what it always was: the words above the group.
            const opens = section.screen && (!section.ownerOnly || user.role === "owner");
            return (
              <div className="nav-section" role="group" aria-label={section.label} key={section.id}>
                {opens ? (
                  <button
                    className="nav-head"
                    aria-current={screen === section.screen ? "page" : undefined}
                    onClick={() => go(section.screen!)}
                  >
                    {section.label}
                  </button>
                ) : (
                  <div className="nav-head nav-head-plain">{section.label}</div>
                )}

                {section.children.map((child) =>
                  "heading" in child ? (
                    <div
                      className="nav-subgroup"
                      role="group"
                      aria-label={child.heading}
                      key={`heading:${child.heading}`}
                    >
                      <div className="nav-subhead">{child.heading}</div>
                      {child.children.map((item) => navItem(item, "nav-child nav-grandchild"))}
                    </div>
                  ) : (
                    navItem(child, "nav-child")
                  ),
                )}
              </div>
            );
          })}
        </div>

        <div className="presence">
          {others.length > 0 ? (
            <>
              <span className="dot" />
              {presenceText(others.map((one) => one.display_name))}
            </>
          ) : (
            <span className="muted">
              <Trans>Only you right now</Trans>
            </span>
          )}
          <div style={{ marginTop: 8 }}>
            {/* Your name is the way into your own settings -- the same place
                every other app puts them, and the reason nobody hunts for it. */}
            {/* Closes the drawer as every page button does: on a phone the
                drawer sits above the account sheet and would cover it. */}
            <button
              className="link"
              onClick={() => {
                setNavOpen(false);
                setProfileOpen(true);
              }}
            >
              {user.display_name}
            </button>
            <button className="link" onClick={() => signOut.mutate()}>
              <Trans comment="Button on the app's menu and frame. See GLOSSARY.md">Sign out</Trans>
            </button>
          </div>
        </div>
      </nav>

      {/* The rail the desktop menu folds against. The whole strip is the
          button, not just the chevron on it: a 14px target is only hittable
          because it runs the height of the window. Hidden on a phone, where
          the drawer above is the menu. */}
      <button
        className="nav-rail"
        aria-controls="main-nav"
        aria-expanded={!navCollapsed}
        aria-label={navCollapsed ? t`Show the menu` : t`Hide the menu`}
        title={navCollapsed ? t`Show the menu` : t`Hide the menu`}
        onClick={() => setNavCollapsed(!navCollapsed)}
      >
        <span aria-hidden="true">{navCollapsed ? "\u203A" : "\u2039"}</span>
      </button>

      <main className="content">
        {/* Above every screen, for the owners: they are who can put the right
            key back, and who the members it locks out will ask (#287). */}
        {user.role === "owner" && <RecoveryModeBanner />}
        {/* And for everybody, about themselves: a new authenticator still owed,
            which a reload on the sign-in screen otherwise left unsaid. */}
        <ReenrolmentDue user={user} onOpen={() => setProfileOpen(true)} />
        {/* Owners only, and only for what somebody else did (#286): the
            price of resets that need no step-up is that they are seen. */}
        <SignInNotice user={user} onOpen={() => go("admin")} />
        <ScreenBoundary resetKey={`${screen}:${household.id}`}>
        {/* The register has an Import button of its own, and navigation lives
            here rather than in a screen. `onGo` takes the screen's name as a
            string, so the cast is the boundary: an unknown name would land on
            a screen that renders nothing, which is why this list is the only
            place a name is coined. */}
        {screen === "register" && (
          <Register
            household={household}
            preset={registerPreset ?? undefined}
            onGo={(to) => go(to as Screen)}
          />
        )}
        {screen === "accounts" && <Accounts household={household} onOpenRegister={openRegister} />}
        {screen === "payees" && <Payees household={household} />}
        {screen === "categories" && <Categories household={household} />}
        {screen === "receipts" && <Receipts household={household} />}
        {screen === "reports" && (
          <Reports household={household} onOpen={(key) => go(reportScreen(key))} />
        )}
        {openReport && (
          <Report
            household={household}
            screen={screen as ReportScreen}
            onBack={() => go("reports")}
            onOpenRegister={openRegister}
          />
        )}
        {screen === "household" && (
          <HouseholdPage
            household={household}
            user={user}
            // The name and the colour are worn by the shell, so a save here has
            // to reach the list this component is holding.
            onChanged={() => households.refetch()}
          />
        )}
        {screen === "import" && <Import household={household} onGo={(to) => go(to as Screen)} />}
        {screen === "rules" && <Rules household={household} />}
        {screen === "payee-categorisation" && <PayeeCategorisation household={household} />}
        {screen === "transfers" && <Transfers household={household} />}
        {screen === "history" && <History household={household} />}
        {screen === "import-guide" && <ImportGuide />}
        {screen === "admin" && user.role === "owner" && <Admin user={user} />}
        {/* Guarded here as well as in the menu. The menu decides what is
            offered; this decides what renders, and a member who reached the
            name some other way gets nothing rather than a screen of 403s. */}
        {screen === "application" && user.role === "owner" && <ApplicationManagement />}
        </ScreenBoundary>
      </main>

      {profileOpen && (
        <Profile user={user} households={list} onClose={() => setProfileOpen(false)} />
      )}
    </div>
  );
}

function FirstHousehold({ onCreated }: { onCreated: () => void }) {
  useLingui();
  const [name, setName] = useState("");
  const [currency, setCurrency] = useState("EUR");

  const create = useMutation({
    mutationFn: () => api.post<Household>("/households", { name, base_currency: currency }),
    onSuccess: onCreated,
  });

  return (
    <div className="centred">
      <h1>
        <Trans comment="Screen title on the app's menu and frame">Your household</Trans>
      </h1>
      <p className="muted small">
        <Trans>
          A household holds the accounts and the register. The currency here is only used for
          totals; each account keeps its own.
        </Trans>
      </p>
      <div className="card">
        <Problem error={create.error} />
        <Field label={t({ message: "Name", comment: "Label of a form field on the app's menu and frame: noun" })}>
          <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
        </Field>
        <p />
        <Field label={t({ message: "Main currency", comment: "Label of a form field on the app's menu and frame" })}>
          <input
            value={currency}
            onChange={(e) => setCurrency(e.target.value.toUpperCase())}
            maxLength={3}
          />
        </Field>
        <p />
        <button className="primary" disabled={!name.trim() || create.isPending} onClick={() => create.mutate()}>
          <Trans comment="Button on the app's menu and frame: verb">Create it</Trans>
        </button>
      </div>
    </div>
  );
}

