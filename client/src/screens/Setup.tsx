/**
 * First boot: the first-run wizard (#458), one code path for every edition.
 *
 * Four chapters -- Welcome (language, overview), Security (setup token, you
 * and your password, Touch ID, a second step, recovery), Your household
 * (household, region and formats, categories, a first account) and Review --
 * laid out as a guide rail (`setup/Frame.tsx`). What an edition walks comes
 * from `useCapabilities()` and the server's `install.edition`, never from a
 * fork: a server asks for the token and a second step; the desktop asks for
 * neither (#320), and a Mac whose shell can prompt offers Touch ID.
 *
 * **Nothing is written until the last step.** The server's sealed blob
 * carries the owner between steps, and `/setup/complete` makes the owner,
 * the second factor, the recovery codes, the household with its categories
 * and the optional first account in one transaction -- or none of it.
 *
 * The language chosen on the first step re-words the whole wizard at once,
 * and it names the starter categories: they are seeded in it, and stay the
 * household's own words afterwards.
 */

import { type ReactNode, useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLingui } from "@lingui/react";
import { t } from "@lingui/core/macro";
import { QRCodeSVG } from "qrcode.react";
import { ApiError, api } from "../lib/api";
import { Field, Problem } from "../components/bits";
import type { User } from "../lib/types";
import { type MadePasskey, PasskeyChoice } from "../components/PasskeyChoice";
import { useCapabilities } from "../lib/capabilities";
import { desktop } from "../lib/desktop";
import { activate, SERVED_LOCALES, storeLocale } from "../lib/i18n";
import { passkeyState, type PasskeyState } from "../lib/passkeys";
import { whyNotHere } from "./Passkeys";
import {
  collator,
  countryName,
  dateTimeFormat,
  displayNames,
  WEEK_STARTS,
  type WeekStart,
  weekdayName,
} from "../lib/locale";
import { format as formatMoney, minorFactor, parse as parseMoney } from "../lib/money";
import { holdImportFile } from "../lib/heldImport";
import { wizardCopy, type WizardCopy } from "./setup/copy";
import { Frame } from "./setup/Frame";
import { Overview } from "./setup/Overview";
import { type Install, TechnicalDetails } from "./setup/TechnicalDetails";
import {
  type AccountOutcome,
  categoryChoice,
  categoryCount,
  countryFor,
  type DateFormat,
  type Edition,
  nextSteps,
  type NextStep,
  type Preset,
  presetIds,
  presetOf,
  type Scenario,
  SCENARIOS,
  type Starter,
  type StepKey,
  stepsFor,
  toggle,
  weekAround,
  writeDate,
} from "./setup/steps";

interface Started {
  blob: string;
  /** Null on the desktop, which enrols no authenticator. */
  otpauth_uri: string | null;
  secret: string | null;
  recovery_codes: string[];
}

interface CountryHint {
  code: string;
  name: string;
  currency: string | null;
  date_format: DateFormat;
  week_start: WeekStart;
}

interface PaletteHint {
  key: string;
  label: string;
  light: string;
  dark: string;
}

export interface SetupState {
  setup_required: boolean;
  /** Where the token was written, from the server. */
  token_path?: string | null;
  /** False on the desktop (#320). */
  token_needed?: boolean;
  countries?: CountryHint[];
  currencies?: string[];
  palettes?: PaletteHint[];
  next_palette?: string;
  starter?: Starter;
  install?: Install;
}

type AccountKind = "current" | "savings" | "credit_card" | "cash";
const ACCOUNT_KINDS: readonly AccountKind[] = ["current", "savings", "credit_card", "cash"];

/** Refusals that mean the wizard has to start over from the token (web) or from you (desktop). */
const START_AGAIN = new Set([
  "setup.server_restarted",
  "setup.session_expired",
  "setup.session_invalid",
  "setup.token_wrong",
  "setup.not_waiting",
]);

function codeOf(error: unknown): string | undefined {
  return error instanceof ApiError ? (error.body?.code as string | undefined) : undefined;
}

/** A language named in itself, without its country: "Español", not "español de España". */
export function ownLanguageName(locale: string): string {
  const base = locale.split("-")[0];
  try {
    const name = new Intl.DisplayNames([base], { type: "language" }).of(base) ?? locale;
    return name.charAt(0).toLocaleUpperCase(base) + name.slice(1);
  } catch {
    return locale;
  }
}

/** "EUR · euro": the code, and its name where the browser knows one. */
function currencyLabel(code: string, names: Intl.DisplayNames | null): string {
  const named = names?.of(code);
  return named && named !== code ? `${code} · ${named}` : code;
}

function todayIso(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** What a seeded word reads in the active language, from the catalogs (`lib/seedWords.ts`). */
function seedWord(i18n: { _: (id: string) => string }, id: string): string {
  return i18n._(id);
}

function scenarioWords(c: WizardCopy, scenario: Scenario, name: string, desktopEdition: boolean) {
  const shown = name.trim() || "…";
  const words = {
    solo: { label: c.sc_solo, text: c.sc_solo_d, next: c.sc_solo_n, suggested: c.nm_solo({ name: shown }) },
    couple: { label: c.sc_couple, text: c.sc_couple_d, next: c.sc_couple_n, suggested: c.nm_couple },
    both: { label: c.sc_both, text: c.sc_both_d, next: desktopEdition ? c.sc_both_nd : c.sc_both_n, suggested: c.nm_both({ name: shown }) },
    family: { label: c.sc_family, text: c.sc_family_d, next: c.sc_family_n, suggested: c.nm_family },
    flat: { label: c.sc_flat, text: c.sc_flat_d, next: c.sc_flat_n, suggested: c.nm_flat },
  }[scenario];
  // No invitations on the desktop: the shared scenarios say so instead.
  if (desktopEdition && scenario !== "solo" && scenario !== "both") return { ...words, next: c.desk_note };
  return words;
}

function kindName(c: WizardCopy, kind: AccountKind): string {
  return { current: c.t_current, savings: c.t_savings, credit_card: c.t_card, cash: c.t_cash }[kind];
}

/** The address a next step opens the app at, read once by the shell (`App.openedAt`). */
const OPENS: Record<NextStep, string> = {
  ynab: "/?open=household",
  account: "/?open=accounts",
  import: "/?open=import",
  shared: "/?open=admin",
  invite: "/?open=admin",
};

export function Setup({ onDone }: { onDone: (user: User) => void }) {
  const { i18n } = useLingui();
  const c = wizardCopy();
  const offered = useCapabilities();
  const state = useQuery({
    queryKey: ["setup", "state"],
    queryFn: () => api.get<SetupState>("/setup/state"),
  });
  const facts = state.data;
  const web = offered.second_factor;
  const edition: Edition = web ? "web" : facts?.install?.edition === "mac" ? "mac" : "windows";
  const touchOffered = !web && offered.device_unlock && desktop()?.unlock === "touch_id";
  const steps = useMemo(() => stepsFor(edition, touchOffered), [edition, touchOffered]);

  const [at, setAt] = useState(0);
  const [furthest, setFurthest] = useState(0);
  const step: StepKey = steps[Math.min(at, steps.length - 1)];
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  // Security
  const [token, setToken] = useState("");
  const [tokenTab, setTokenTab] = useState<"docker" | "podman" | "systemd">("docker");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [started, setStarted] = useState<Started | null>(null);
  const [begunWith, setBegunWith] = useState("");
  const [blob, setBlob] = useState("");
  const [enrolled, setEnrolled] = useState(false);
  const [code, setCode] = useState("");
  const [passkeys, setPasskeys] = useState<PasskeyState | null>(null);
  const [secondStep, setSecondStep] = useState<"passkey" | "totp">("passkey");
  const [passkey, setPasskey] = useState<MadePasskey | null>(null);
  const [touch, setTouch] = useState(false);
  const [stored, setStored] = useState(false);
  const [copied, setCopied] = useState(false);

  // Household
  const [scenario, setScenario] = useState<Scenario>("solo");
  const [householdName, setHouseholdName] = useState("");
  const [nameTyped, setNameTyped] = useState(false);
  const [colour, setColour] = useState<string | null>(null);
  const [country, setCountry] = useState<string | null>(null);
  const [currency, setCurrency] = useState("");
  const [dateFormat, setDateFormat] = useState<DateFormat>("YYYY-MM-DD");
  const [weekStart, setWeekStart] = useState<WeekStart>("monday");
  const [ticked, setTicked] = useState<Set<string> | null>(null);
  const [ynab, setYnab] = useState(false);

  // First account
  const [accountName, setAccountName] = useState("");
  const [accountKind, setAccountKind] = useState<AccountKind>("current");
  const [accountCurrency, setAccountCurrency] = useState<string | null>(null);
  const [openingDate, setOpeningDate] = useState(todayIso);
  const [openingBalance, setOpeningBalance] = useState("");
  const [heldFile, setHeldFile] = useState<File | null>(null);
  const [accountOutcome, setAccountOutcome] = useState<AccountOutcome | null>(null);

  const [finished, setFinished] = useState<User | null>(null);

  useEffect(() => {
    if (!web) return;
    let gone = false;
    void passkeyState().then((found) => {
      if (gone) return;
      setPasskeys(found);
      if (!found.available) setSecondStep("totp");
    });
    return () => {
      gone = true;
    };
  }, [web]);

  // The region's first guess, from the language, until the person picks a country.
  const countries = facts?.countries ?? [];
  const guessed = countryFor(i18n.locale, typeof navigator === "undefined" ? [] : (navigator.languages ?? []));
  const chosenCountry = country ?? (countries.some((one) => one.code === guessed) ? guessed : "");
  const hint = countries.find((one) => one.code === chosenCountry);
  const regionTouched = country !== null;
  // Untouched, the region follows the suggestion; once touched, what was chosen.
  const baseCurrency = currency || hint?.currency || "EUR";
  const chosenDateFormat = regionTouched || currency ? dateFormat : (hint?.date_format ?? dateFormat);
  const chosenWeekStart = regionTouched || currency ? weekStart : (hint?.week_start ?? weekStart);
  const starter = facts?.starter ?? null;
  const ticks = ticked ?? (starter ? presetIds(starter, "recommended") : new Set<string>());
  const suggestedName = scenarioWords(c, scenario, name, !web).suggested;
  const chosenName = nameTyped ? householdName : suggestedName;
  const chosenColour = colour ?? facts?.next_palette ?? "moss";
  const accountCode = accountCurrency ?? baseCurrency;
  const parsedBalance = openingBalance.trim() ? parseMoney(openingBalance, accountCode) : 0;

  function go(index: number) {
    setError(null);
    setAt(index);
    setFurthest((was) => Math.max(was, index));
  }
  const next = () => go(at + 1);
  const back = () => go(Math.max(0, at - 1));

  async function run(work: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (problem) {
      setError(problem);
      // A restart rotates the token and spoils every blob made under the old
      // one: back to where a new one is asked for, with the reason shown.
      if (START_AGAIN.has(codeOf(problem) ?? "")) {
        const restart = steps.indexOf(web ? "token" : "you");
        setStarted(null);
        setBegunWith("");
        setEnrolled(false);
        setPasskey(null);
        setAt(restart);
        setFurthest(restart);
      }
    } finally {
      setBusy(false);
    }
  }

  const chooseLanguage = async (locale: string) => {
    storeLocale(locale);
    await activate(locale);
  };

  /** Leaving "you": the server checks the answers and seals the owner into a blob. */
  const begin = () =>
    run(async () => {
      const signature = JSON.stringify([token, email, name, password]);
      if (started && signature === begunWith) {
        next();
        return;
      }
      const result = await api.post<Started>("/setup/begin", { token, email, display_name: name, password });
      setStarted(result);
      setBlob(result.blob);
      setBegunWith(signature);
      setEnrolled(false);
      setPasskey(null);
      setCode("");
      setStored(false);
      next();
    });

  const enrol = () =>
    run(async () => {
      if (secondStep === "passkey") {
        next();
        return;
      }
      if (!enrolled) {
        const result = await api.post<{ blob: string }>("/setup/enrol", { blob, code });
        setBlob(result.blob);
        setEnrolled(true);
        setPasskey(null);
        // The secret has done its job: it does not sit in state until the
        // page closes (#198).
        setCode("");
        setStarted((was) => was && { ...was, secret: "", otpauth_uri: "" });
      }
      next();
    });

  const create = () =>
    run(async () => {
      if (!starter) return;
      const made = web && secondStep === "passkey" ? passkey : null;
      const categories = ynab ? categoryChoice(starter, presetIds(starter, "ynab")) : categoryChoice(starter, ticks);
      const user = await api.post<User>("/setup/complete", {
        blob: made ? made.blob : blob,
        codes_saved: stored,
        ...(made ? { second_step: "passkey", credential: made.credential } : {}),
        household: {
          name: chosenName.trim(),
          colour: chosenColour,
          base_currency: baseCurrency,
          date_format: chosenDateFormat,
          week_start: chosenWeekStart,
          locale: i18n.locale,
          ...categories,
        },
        ...(accountOutcome === "made"
          ? {
              account: {
                name: accountName.trim(),
                kind: accountKind,
                currency: accountCode,
                opening_balance: parsedBalance ?? 0,
                opening_date: openingDate,
              },
            }
          : {}),
      });
      if (touch) {
        // After the commit, with the session it gave: the shell takes the
        // secret off the answer (`/me/device-unlock`, #432). A failure here
        // leaves Touch ID off and the household made; Your account offers it.
        try {
          await api.post("/me/device-unlock", { password, method: "touch_id" });
        } catch (problem) {
          setError(problem);
        }
      }
      setPassword("");
      holdImportFile(heldFile);
      setFinished(user);
    });

  const open = (where: NextStep | null) => {
    if (!finished) return;
    if (where) window.history.replaceState(null, "", OPENS[where]);
    onDone(finished);
  };

  if (state.isPending)
    return (
      <div className="centred muted">{t({ message: "Loading…", comment: "Text on the app's menu and frame" })}</div>
    );

  // ------------------------------------------------------------------------- //
  // Each step: its title, lead, explanation, body, and whether it may go on
  // ------------------------------------------------------------------------- //

  let title = "";
  let lead: ReactNode = "";
  let about: ReactNode = "";
  let body: ReactNode = null;
  let valid = true;
  let onContinue: () => void = next;

  switch (step) {
    case "welcome": {
      title = c.h_welcome;
      lead = c.l_welcome;
      about = c.i_welcome;
      body = (
        <fieldset className="wizard-choices">
          <legend className="sr-only">{c.language}</legend>
          {SERVED_LOCALES.map((locale) => (
            <label key={locale} className={`wizard-choice ${i18n.locale === locale ? "chosen" : ""}`}>
              <input
                type="radio"
                name="language"
                value={locale}
                checked={i18n.locale === locale}
                onChange={() => void chooseLanguage(locale)}
              />
              <span lang={locale}>{ownLanguageName(locale)}</span>
              <span className="muted small mono">{locale}</span>
            </label>
          ))}
          <p className="muted small">{c.saved_note}</p>
        </fieldset>
      );
      break;
    }
    case "overview": {
      title = c.h_overview;
      lead = c.l_overview;
      about = c.i_overview;
      body = <Overview c={c} />;
      break;
    }
    case "token": {
      const path = facts?.token_path ?? "/var/lib/spend-tracker/setup-token";
      // The image has no shell and no `cat`, so the commands use the
      // interpreter that is always there.
      const commands = {
        docker: `docker compose exec app python -c "print(open('${path}').read())"`,
        podman: `podman compose exec app python -c "print(open('${path}').read())"`,
        systemd: `sudo cat ${path}`,
      };
      title = c.h_token;
      lead = c.l_token;
      about = c.i_token;
      valid = Boolean(token.trim());
      body = (
        <>
          <div className="wizard-tabs" role="tablist" aria-label={c.token_how}>
            {(["docker", "podman", "systemd"] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                role="tab"
                aria-selected={tokenTab === tab}
                className={tokenTab === tab ? "chosen" : ""}
                onClick={() => setTokenTab(tab)}
              >
                {{ docker: "Docker", podman: "Podman", systemd: "systemd" }[tab]}
              </button>
            ))}
          </div>
          <pre className="wizard-command mono small" role="tabpanel">
            {commands[tokenTab]}
          </pre>
          <Field label={c.token}>
            <input
              name="setup-token"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              spellCheck={false}
              autoComplete="off"
            />
          </Field>
        </>
      );
      break;
    }
    case "you": {
      title = c.h_you;
      lead = c.l_you;
      about = c.i_you;
      valid = Boolean(name.trim() && email.trim() && password);
      onContinue = begin;
      const filled = Math.min(password.length / 24, 1);
      body = (
        <>
          <Field label={c.name}>
            <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
          </Field>
          {/* The desktop still signs in with an email address (#320), so it is
              asked on every edition until that changes. */}
          <Field label={c.email}>
            <input
              type="email"
              name="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="username"
            />
          </Field>
          <Field label={c.password}>
            <div className="wizard-password">
              <input
                type={showPassword ? "text" : "password"}
                name="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
              />
              <button type="button" onClick={() => setShowPassword((was) => !was)} aria-pressed={showPassword}>
                {showPassword ? c.hide : c.show}
              </button>
            </div>
          </Field>
          <div className={`wizard-meter ${password.length >= 12 ? "long" : ""}`} aria-hidden="true">
            <span style={{ width: `${filled * 100}%` }} />
          </div>
          <p className="muted small">
            {c.pw_hint} {password ? c.pw_count({ n: password.length }) : null}
          </p>
        </>
      );
      break;
    }
    case "touch": {
      title = c.h_touch;
      lead = c.l_touch;
      about = c.i_touch;
      body = (
        <label className="wizard-switch">
          <input type="checkbox" role="switch" checked={touch} onChange={(e) => setTouch(e.target.checked)} />
          <span>{c.touch_label}</span>
        </label>
      );
      break;
    }
    case "auth": {
      title = c.h_auth;
      lead = c.l_auth;
      about = c.i_auth;
      onContinue = enrol;
      valid = secondStep === "passkey" ? Boolean(passkey) : enrolled || code.trim().length >= 6;
      const canPasskey = passkeys?.available ?? false;
      body = (
        <fieldset className="wizard-choices">
          <legend className="sr-only">{c.s_auth}</legend>
          {canPasskey ? (
            <div className={`wizard-choice block ${secondStep === "passkey" ? "chosen" : ""}`}>
              <label className="wizard-choice-head">
                <input
                  type="radio"
                  name="second-step"
                  checked={secondStep === "passkey"}
                  onChange={() => setSecondStep("passkey")}
                />
                <span>
                  <strong>{c.auth_pk}</strong>
                  <span className="muted small"> {c.auth_pk_d}</span>
                </span>
              </label>
              {secondStep === "passkey" && started ? (
                <PasskeyChoice
                  bare
                  offerPath="/setup/passkey/options"
                  body={{ blob }}
                  made={passkey}
                  onMade={setPasskey}
                />
              ) : null}
            </div>
          ) : passkeys ? (
            <p className="small muted">{whyNotHere(passkeys)}</p>
          ) : null}
          <div className={`wizard-choice block ${secondStep === "totp" ? "chosen" : ""}`}>
            <label className="wizard-choice-head">
              <input
                type="radio"
                name="second-step"
                checked={secondStep === "totp"}
                onChange={() => setSecondStep("totp")}
              />
              <span>
                <strong>{c.auth_app}</strong>
                <span className="muted small"> {c.auth_app_d}</span>
              </span>
            </label>
            {secondStep === "totp" && started?.otpauth_uri && !enrolled ? (
              <>
                <div className="wizard-qr">
                  <QRCodeSVG value={started.otpauth_uri} size={168} />
                </div>
                <p className="small muted">
                  {c.cant_scan} <span className="mono">{started.secret}</span>
                </p>
                <Field label={c.six}>
                  <input
                    name="one-time-code"
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                    inputMode="numeric"
                    maxLength={6}
                    autoComplete="one-time-code"
                  />
                </Field>
              </>
            ) : secondStep === "totp" && enrolled ? (
              <p className="small" role="status">
                <span aria-hidden="true">✓ </span>
                {c.auth_app}
              </p>
            ) : null}
          </div>
        </fieldset>
      );
      break;
    }
    case "recovery": {
      const codes = started?.recovery_codes ?? [];
      title = c.h_recovery;
      lead = web ? c.l_recovery_web : c.l_recovery;
      about = c.i_recovery;
      valid = stored && codes.length > 0;
      const text = codes.join("\n");
      body = (
        <>
          <div className="codes" aria-label={c.recover_codes}>
            {codes.map((one) => (
              <span key={one}>{one}</span>
            ))}
          </div>
          <div className="row" style={{ gap: 8 }}>
            <button
              type="button"
              onClick={() => {
                void navigator.clipboard?.writeText(text).then(() => setCopied(true));
              }}
            >
              {c.copy}
            </button>
            <button
              type="button"
              onClick={() => {
                const link = document.createElement("a");
                link.href = URL.createObjectURL(new Blob([`${text}\n`], { type: "text/plain" }));
                link.download = "recovery-codes.txt";
                link.click();
                URL.revokeObjectURL(link.href);
              }}
            >
              {c.savefile}
            </button>
            {copied ? (
              <span className="small muted" role="status">
                {c.copied}
              </span>
            ) : null}
          </div>
          <p />
          <label className="small">
            <input
              type="checkbox"
              checked={stored}
              onChange={(e) => setStored(e.target.checked)}
              style={{ width: "auto", marginRight: 8 }}
            />
            {c.stored}
          </label>
        </>
      );
      break;
    }
    case "household": {
      title = c.h_household;
      lead = c.l_household;
      about = c.i_household;
      valid = Boolean(chosenName.trim());
      const words = scenarioWords(c, scenario, name, !web);
      body = (
        <>
          <p className="wizard-rule">{c.hh_rule}</p>
          <fieldset className="wizard-choices">
            <legend>{c.hh_who}</legend>
            {SCENARIOS.map((one) => {
              const shown = scenarioWords(c, one, name, !web);
              return (
                <label key={one} className={`wizard-choice block wizard-choice-head ${scenario === one ? "chosen" : ""}`}>
                  <input type="radio" name="scenario" checked={scenario === one} onChange={() => setScenario(one)} />
                  <span>
                    <strong>{shown.label}</strong>
                    <span className="muted small"> {shown.text}</span>
                  </span>
                </label>
              );
            })}
          </fieldset>
          <p className="wizard-accent small">{words.next}</p>
          <Field label={c.hh_name}>
            <input
              value={chosenName}
              onChange={(e) => {
                setNameTyped(true);
                setHouseholdName(e.target.value);
              }}
            />
          </Field>
          <div className="field">
            <span>{c.colour}</span>
            <div className="swatches">
              {(facts?.palettes ?? []).map((one) => (
                <button
                  key={one.key}
                  type="button"
                  className={one.key === chosenColour ? "swatch chosen" : "swatch"}
                  aria-pressed={one.key === chosenColour}
                  onClick={() => setColour(one.key)}
                  title={one.label}
                >
                  <span className="swatch-chips">
                    <span style={{ background: one.light }} />
                    <span style={{ background: one.dark }} />
                  </span>
                  {one.label}
                </button>
              ))}
            </div>
          </div>
        </>
      );
      break;
    }
    case "region": {
      title = c.h_region;
      lead = c.l_region;
      about = c.i_region;
      valid = Boolean(baseCurrency);
      const names = collator();
      const listed = [...countries]
        .map((one) => ({ ...one, shown: countryName(one.code, one.name) }))
        .sort((a, b) => names.compare(a.shown, b.shown));
      const currencyNames = displayNames("currency");
      const today = todayIso();
      const [first, last] = weekAround(new Date(`${today}T12:00:00Z`), chosenWeekStart);
      const dayMonth = dateTimeFormat({ weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
      const factor = minorFactor(baseCurrency) / 100;
      body = (
        <>
          <Field label={c.country}>
            <select
              value={chosenCountry}
              onChange={(e) => {
                const picked = countries.find((one) => one.code === e.target.value);
                setCountry(e.target.value);
                if (picked?.currency) setCurrency(picked.currency);
                if (picked) {
                  setDateFormat(picked.date_format);
                  setWeekStart(picked.week_start);
                }
              }}
            >
              {!chosenCountry ? <option value="" /> : null}
              {listed.map((one) => (
                <option key={one.code} value={one.code}>
                  {one.shown}
                </option>
              ))}
            </select>
          </Field>
          <Field label={c.currency}>
            <select
              value={baseCurrency}
              onChange={(e) => {
                setCurrency(e.target.value);
                setDateFormat(chosenDateFormat);
                setWeekStart(chosenWeekStart);
              }}
            >
              {(facts?.currencies ?? []).map((code) => (
                <option key={code} value={code}>
                  {currencyLabel(code, currencyNames)}
                </option>
              ))}
            </select>
          </Field>
          <div className="wizard-pair">
            <Field label={c.datefmt}>
              <select
                value={chosenDateFormat}
                onChange={(e) => {
                  setCurrency(baseCurrency);
                  setWeekStart(chosenWeekStart);
                  setDateFormat(e.target.value as DateFormat);
                }}
              >
                <option value="YYYY-MM-DD">{t({ message: "YYYY-MM-DD", comment: "Option in a dropdown on the Admin screen" })}</option>
                <option value="DD/MM/YYYY">{t`DD/MM/YYYY`}</option>
                <option value="MM/DD/YYYY">{t`MM/DD/YYYY`}</option>
              </select>
            </Field>
            <Field label={c.weekstart}>
              <select
                value={chosenWeekStart}
                onChange={(e) => {
                  setCurrency(baseCurrency);
                  setDateFormat(chosenDateFormat);
                  setWeekStart(e.target.value as WeekStart);
                }}
              >
                {WEEK_STARTS.map((day) => {
                  const { name: dayName, lang } = weekdayName(day);
                  return (
                    <option key={day} value={day} lang={lang}>
                      {dayName}
                    </option>
                  );
                })}
              </select>
            </Field>
          </div>
          <div className="wizard-preview">
            <div className="wizard-preview-head">{c.preview}</div>
            <div>{dateTimeFormat({ dateStyle: "long", timeZone: "UTC" }).format(new Date(`${today}T12:00:00Z`))}</div>
            <table className="small">
              <tbody>
                <tr>
                  <td className="mono">{writeDate(today, chosenDateFormat)}</td>
                  <td>{seedWord(i18n, "seed.category.groceries")}</td>
                  <td className="num">{formatMoney(Math.round(-123456 * factor), baseCurrency)}</td>
                </tr>
                <tr>
                  <td className="mono">{writeDate(today, chosenDateFormat)}</td>
                  <td>{seedWord(i18n, "seed.category.salary")}</td>
                  <td className="num">+{formatMoney(Math.round(325000 * factor), baseCurrency)}</td>
                </tr>
              </tbody>
            </table>
            <div className="small">
              {c.thisweek}: {dayMonth.format(first)} – {dayMonth.format(last)}
            </div>
          </div>
        </>
      );
      break;
    }
    case "categories": {
      title = c.h_categories;
      lead = c.l_categories;
      about = c.i_categories;
      if (!starter) break;
      const preset = presetOf(starter, ticks, ynab);
      const count = categoryCount(starter, ticks);
      const lang = ownLanguageName(i18n.locale);
      const presets: { key: Preset; label: string; text: string }[] = [
        { key: "recommended", label: c.p_rec, text: c.p_rec_d },
        { key: "essentials", label: c.p_ess, text: c.p_ess_d },
        { key: "empty", label: c.p_empty, text: c.p_empty_d },
        ...(offered.ynab_import ? [{ key: "ynab" as const, label: c.p_ynab, text: c.p_ynab_d }] : []),
      ];
      body = (
        <>
          <div className="wizard-presets">
            {presets.map((one) => (
              <button
                key={one.key}
                type="button"
                className={`wizard-preset ${preset === one.key ? "chosen" : ""}`}
                aria-pressed={preset === one.key}
                onClick={() => {
                  setYnab(one.key === "ynab");
                  if (one.key !== "ynab") setTicked(presetIds(starter, one.key));
                }}
              >
                <strong>{one.label}</strong>
                <span className="muted small">{one.text}</span>
              </button>
            ))}
          </div>
          <p className="small">
            {ynab ? c.cat_lang_ynab({ lang }) : c.cat_lang({ n: count, lang })}{" "}
            <button type="button" className="link" onClick={() => go(0)}>
              {c.change_lang}
            </button>
          </p>
          {ynab ? (
            <p className="wizard-accent small">{c.ynab_note}</p>
          ) : (
            <ul className="wizard-tree">
              {starter.tree.map(([group, ids]) => (
                <li key={group}>
                  <Tick
                    id={group}
                    label={seedWord(i18n, group)}
                    starter={starter}
                    ticks={ticks}
                    locked={c.locked}
                    onToggle={(id) => setTicked(toggle(starter, ticks, id))}
                    strong
                  />
                  <ul>
                    {ids.map((id) => (
                      <li key={id}>
                        <Tick
                          id={id}
                          label={seedWord(i18n, id)}
                          starter={starter}
                          ticks={ticks}
                          locked={c.locked}
                          onToggle={(one) => setTicked(toggle(starter, ticks, one))}
                        />
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          )}
        </>
      );
      break;
    }
    case "account": {
      title = c.h_account;
      lead = c.l_account;
      about = c.i_account;
      valid = Boolean(accountName.trim()) && parsedBalance !== null && Boolean(openingDate) && openingDate <= todayIso();
      onContinue = () => {
        setAccountOutcome("made");
        next();
      };
      const currencyNames = displayNames("currency");
      body = (
        <>
          <ul className="wizard-points">
            <li>
              <span aria-hidden="true">📅</span>
              <span>
                <strong>{c.acc_h1}</strong> <span className="muted">{c.acc_h1_d}</span>
              </span>
            </li>
            <li>
              <span aria-hidden="true">🏦</span>
              <span>
                <strong>{c.acc_h2}</strong> <span className="muted">{c.acc_h2_d}</span>
              </span>
            </li>
            <li>
              <span aria-hidden="true">✏️</span>
              <span>
                <strong>{c.acc_h3}</strong> <span className="muted">{c.acc_h3_d}</span>
              </span>
            </li>
          </ul>
          <Field label={c.acc_name}>
            <input value={accountName} placeholder={c.acc_name_ph} onChange={(e) => setAccountName(e.target.value)} />
          </Field>
          <div className="wizard-pair">
            <Field label={c.acc_type}>
              <select value={accountKind} onChange={(e) => setAccountKind(e.target.value as AccountKind)}>
                {ACCOUNT_KINDS.map((kind) => (
                  <option key={kind} value={kind}>
                    {kindName(c, kind)}
                  </option>
                ))}
              </select>
            </Field>
            <div>
              <Field label={c.acc_cur}>
                <select value={accountCode} onChange={(e) => setAccountCurrency(e.target.value)}>
                  {(facts?.currencies ?? []).map((code) => (
                    <option key={code} value={code}>
                      {currencyLabel(code, currencyNames)}
                    </option>
                  ))}
                </select>
              </Field>
              <button
                type="button"
                className="link small"
                onClick={() => {
                  setAccountOutcome("no_currency");
                  next();
                }}
              >
                {c.acc_nocur} →
              </button>
            </div>
          </div>
          <div className="wizard-pair">
            <div>
              <Field label={c.acc_date}>
                <input type="date" value={openingDate} max={todayIso()} onChange={(e) => setOpeningDate(e.target.value)} />
              </Field>
              <p className="muted small wizard-hint">{c.acc_date_h}</p>
            </div>
            <div>
              <Field label={c.acc_bal}>
                <input inputMode="decimal" value={openingBalance} onChange={(e) => setOpeningBalance(e.target.value)} />
              </Field>
              <p className="muted small wizard-hint">{c.acc_bal_h}</p>
            </div>
          </div>
          <label
            className="wizard-drop"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              const dropped = e.dataTransfer.files?.[0];
              if (dropped) setHeldFile(dropped);
            }}
          >
            <input
              type="file"
              accept=".csv,.ofx,.qfx,text/csv"
              className="sr-only"
              onChange={(e) => setHeldFile(e.target.files?.[0] ?? null)}
            />
            <strong>{c.acc_file}</strong>
            <span className="muted small">{heldFile ? heldFile.name : c.acc_file_d}</span>
          </label>
        </>
      );
      break;
    }
    case "finish": {
      title = finished ? c.done : c.h_finish;
      lead = finished ? c.done_l : c.l_finish;
      about = c.i_finish;
      body = finished ? (
        <FinishedNext
          c={c}
          steps={nextSteps({
            scenario,
            invitations: offered.invitations,
            account: accountOutcome ?? "skipped",
            ynab,
          })}
          noCurrency={accountOutcome === "no_currency"}
          onOpen={open}
        />
      ) : (
        <Review
          c={c}
          rows={reviewRows()}
          onChange={(to) => go(steps.indexOf(to))}
        />
      );
      break;
    }
  }

  function reviewRows(): [StepKey, string, string][] {
    const rows: [StepKey, string, string][] = [
      ["welcome", c.language, ownLanguageName(i18n.locale)],
      ["you", c.s_you, [name, email].filter(Boolean).join(" · ")],
    ];
    if (steps.includes("touch"))
      rows.push([
        "touch",
        c.s_touch,
        touch
          ? t({ message: "On", context: "unlock state", comment: "State on the Your account panel: Touch ID unlock is turned on" })
          : t({ message: "Off", context: "unlock state", comment: "State on the Your account panel: Touch ID unlock is turned off" }),
      ]);
    if (steps.includes("auth")) rows.push(["auth", c.s_auth, secondStep === "passkey" ? c.auth_pk : c.auth_app]);
    rows.push(["household", c.hh_name, chosenName]);
    const regionName = hint ? countryName(hint.code, hint.name) : "";
    rows.push([
      "region",
      c.s_region,
      [regionName, baseCurrency, chosenDateFormat, weekdayName(chosenWeekStart).name].filter(Boolean).join(" · "),
    ]);
    if (starter) rows.push(["categories", c.s_categories, ynab ? c.p_ynab : c.picked({ n: categoryCount(starter, ticks) })]);
    rows.push([
      "account",
      c.s_account,
      accountOutcome === "made"
        ? [
            accountName.trim(),
            kindName(c, accountKind),
            accountCode,
            formatMoney(parsedBalance ?? 0, accountCode),
            c.acc_from({ date: writeDate(openingDate, chosenDateFormat) }),
          ].join(" · ")
        : accountOutcome === "no_currency"
          ? c.acc_nocur_row
          : c.acc_skipped,
    ]);
    return rows;
  }

  const last = step === "finish";
  const nav = finished ? (
    <>
      <span />
      <button type="button" className="primary" onClick={() => open(null)}>
        {c.cont}
      </button>
    </>
  ) : (
    <>
      {at > 0 ? (
        <button type="button" onClick={back} disabled={busy}>
          {c.back}
        </button>
      ) : (
        <span />
      )}
      <span className="wizard-nav-end">
        {step === "account" ? (
          <button
            type="button"
            className="link"
            onClick={() => {
              setAccountOutcome("skipped");
              next();
            }}
          >
            {c.skip}
          </button>
        ) : null}
        <button
          type="button"
          className="primary"
          disabled={busy || !valid || (last && !starter)}
          onClick={last ? create : onContinue}
        >
          {last ? c.create : c.cont}
        </button>
      </span>
    </>
  );

  return (
    <Frame
      c={c}
      steps={steps}
      at={at}
      furthest={furthest}
      done={Boolean(finished)}
      onJump={go}
      title={title}
      lead={lead}
      about={about}
      nav={nav}
    >
      <Problem error={error} />
      {body}
      {last ? (
        <>
          {!finished ? <p className="muted small">{c.cat_later}</p> : null}
          <TechnicalDetails c={c} edition={edition} install={facts?.install ?? null} />
        </>
      ) : null}
    </Frame>
  );
}

function Tick({
  id,
  label,
  starter,
  ticks,
  locked,
  onToggle,
  strong = false,
}: {
  id: string;
  label: string;
  starter: Starter;
  ticks: ReadonlySet<string>;
  locked: string;
  onToggle: (id: string) => void;
  strong?: boolean;
}) {
  const required = starter.required.includes(id);
  return (
    <label className="wizard-tick">
      <input type="checkbox" checked={ticks.has(id)} disabled={required} onChange={() => onToggle(id)} />
      {strong ? <strong>{label}</strong> : <span>{label}</span>}
      {required ? <span className="wizard-tag">{locked}</span> : null}
    </label>
  );
}

function Review({
  c,
  rows,
  onChange,
}: {
  c: WizardCopy;
  rows: [StepKey, string, string][];
  onChange: (step: StepKey) => void;
}) {
  return (
    <dl className="wizard-review">
      {rows.map(([step, label, value]) => (
        <div key={step}>
          <dt>{label}</dt>
          <dd>{value}</dd>
          <dd>
            <button type="button" className="link" onClick={() => onChange(step)} aria-label={`${c.change}: ${label}`}>
              {c.change}
            </button>
          </dd>
        </div>
      ))}
    </dl>
  );
}

function FinishedNext({
  c,
  steps,
  noCurrency,
  onOpen,
}: {
  c: WizardCopy;
  steps: NextStep[];
  noCurrency: boolean;
  onOpen: (where: NextStep) => void;
}) {
  const words: Record<NextStep, [string, string]> = {
    ynab: [c.p_ynab, c.p_ynab_d],
    account: [c.next_account, noCurrency ? c.next_account_nocur : c.next_account_d],
    import: [c.next_import, c.next_import_d],
    shared: [c.next_shared, c.next_shared_d],
    invite: [c.next_invite, c.next_invite_d],
  };
  return (
    <ol className="wizard-next">
      {steps.map((one) => (
        <li key={one}>
          <button type="button" className="link" onClick={() => onOpen(one)}>
            {words[one][0]}
          </button>
          <span className="muted small">{words[one][1]}</span>
        </li>
      ))}
    </ol>
  );
}

