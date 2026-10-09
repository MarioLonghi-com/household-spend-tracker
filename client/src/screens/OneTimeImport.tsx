/**
 * One-time Import (#183): bringing a household's history over from another app.
 *
 * A section at the bottom of the household page, opening a wizard. YNAB is the
 * only workflow so far; the section says where to ask for another.
 *
 * Everything the wizard decides is React state and nothing else -- not
 * localStorage, not the query cache -- and the server keeps nothing between
 * calls, so every call carries the source again (see `ynab/calls.ts`). That is
 * also what keeps a YNAB token short-lived: it lives in this component, goes up
 * as a form field, and is dropped when the import finishes or the wizard
 * closes. The four calls' mutations carry it too, as their `variables`, and
 * TanStack keeps a finished mutation for five minutes after its component
 * unmounts -- so each says `gcTime: 0`, and closing the wizard drops them (#93). Nothing here decides whether a row can be imported; the preview is
 * the server running the real import and rolling it back.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Field, Panel, Problem } from "../components/bits";
import type { Household } from "../lib/types";
import * as calls from "./ynab/calls";
import {
  accountFromSuggestion,
  categoryFromSuggestion,
  MapAccounts,
  MapCategories,
  range,
} from "./ynab/mapping";
import { Counts, Duplicates, NotImportedTable, Report, UnpairedTransfers } from "./ynab/results";
import type {
  AccountChoice,
  Analysis,
  CategoryChoice,
  ImportPlan,
  ImportReport,
  Source,
  Via,
  YnabPlan,
} from "./ynab/types";
import { YNAB_STEP_LABELS } from "../lib/labels";
import { plural, t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { formatCount } from "../lib/locale";

export const NEW_ISSUE_URL = "https://github.com/MarioLonghi-com/household-spend-tracker/issues/new";
export const YNAB_DEVELOPER_URL = "https://app.ynab.com/settings/developer";

export function OneTimeImport({
  household,
}: {
  household: Household;
}) {
  const [open, setOpen] = useState(false);
  // A stray backdrop click or Escape would otherwise drop every mapping made so far.
  const inProgress = useRef(false);
  const dismiss = () => {
    if (inProgress.current && !window.confirm(t`Close the import? Everything chosen so far will be lost.`)) return;
    inProgress.current = false;
    setOpen(false);
  };
  return (
    <section className="card" aria-labelledby="one-time-import-title">
      <h2 id="one-time-import-title" className="section-title">
        <Trans comment="Heading on the one-time import">
          One-time Import
        </Trans>
      </h2>
      <p className="small">
        <Trans>
          Bring your history over from the app you used before, once: transactions, accounts,
          categories and transfers, mapped onto what this household already has. YNAB is the one
          workflow so far. Other budgeting and spend tracker app import workflows can be added to
          this, just suggest it via{" "}
          <a href={NEW_ISSUE_URL} target="_blank" rel="noopener noreferrer">
            GitHub
          </a>
          .
        </Trans>
      </p>
      <button type="button" className="primary" onClick={() => setOpen(true)}>
        <Trans>
          Start a one-time import
        </Trans>
      </button>
      {open ? (
        <Panel title={t({ message: "One-time Import", comment: "Title of a panel on the one-time import" })} onClose={dismiss} config wide>
          {/* Unmounting the wizard is what drops the token and the file. */}
          <YnabWizard
            household={household}
            onClose={dismiss}
            onProgress={(busy) => {
              inProgress.current = busy;
            }}
          />
        </Panel>
      ) : null}
    </section>
  );
}

// --------------------------------------------------------------------------- //
// The wizard
// --------------------------------------------------------------------------- //

type Step =
  | "source"
  | "connect"
  | "plan"
  | "review"
  | "accounts"
  | "categories"
  | "options"
  | "preview"
  | "report";

const STEP_LABELS: Record<Step, string> = YNAB_STEP_LABELS;

const COMMON_CURRENCIES = ["GBP", "EUR", "USD", "CAD", "AUD", "NZD", "CHF", "SEK", "NOK", "DKK", "JPY"];

function YnabWizard({
  household,
  onClose,
  onProgress,
}: {
  household: Household;
  onClose: () => void;
  onProgress?: (inProgress: boolean) => void;
}) {
  const client = useQueryClient();
  const hid = household.id;

  const [step, setStep] = useState<Step>("source");
  useEffect(() => {
    onProgress?.(step !== "source" && step !== "report");
  }, [step, onProgress]);

  // Where the rows come from.
  const [via, setVia] = useState<Via>("csv");
  const [file, setFile] = useState<File | null>(null);
  const [token, setToken] = useState("");
  const [plans, setPlans] = useState<YnabPlan[] | null>(null);
  const [planId, setPlanId] = useState<string>("");

  // What the server found, and what the user decided about it.
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [currency, setCurrency] = useState<string | null>(null);
  const [dateFormat, setDateFormat] = useState<string | null>(null);
  const [accountMap, setAccountMap] = useState<Record<string, AccountChoice>>({});
  const [categoryMap, setCategoryMap] = useState<Record<string, CategoryChoice>>({});
  const [flags, setFlags] = useState<"memo" | "ignore">("memo");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [startingBalance, setStartingBalance] = useState<"import" | "skip">("import");
  const [acknowledged, setAcknowledged] = useState(false);

  // The dry run, the duplicates decided on it, and the real thing.
  const [dryRun, setDryRun] = useState<ImportReport | null>(null);
  const [importAll, setImportAll] = useState(false);
  const [importAnyway, setImportAnyway] = useState<Set<string>>(new Set());
  const [report, setReport] = useState<ImportReport | null>(null);

  const source: Source | null =
    via === "csv"
      ? file
        ? { via: "csv", file }
        : null
      : token.trim() && planId
        ? { via: "api", token: token.trim(), planId }
        : null;

  const steps: Step[] = [
    "source",
    "connect",
    ...(via === "api" ? (["plan"] as Step[]) : []),
    "review",
    "accounts",
    "categories",
    "options",
    "preview",
    "report",
  ];
  const at = steps.indexOf(step);

  /** A new source makes everything worked out from the old one stale. */
  function forgetAnalysis() {
    setAnalysis(null);
    setCurrency(null);
    setDateFormat(null);
    setAccountMap({});
    setCategoryMap({});
    forgetDryRun();
  }
  function forgetDryRun() {
    setDryRun(null);
    setImportAll(false);
    setImportAnyway(new Set());
  }

  const plansCall = useMutation({
    gcTime: 0,
    mutationFn: (key: string) => calls.listPlans(hid, key),
    onSuccess: (data) => {
      setPlans(data.plans);
      if (!data.plans.some((plan) => plan.id === planId)) setPlanId("");
      setStep("plan");
    },
  });

  const analyseCall = useMutation({
    gcTime: 0,
    mutationFn: (vars: { source: Source; currency: string | null; dateFormat: string | null }) =>
      calls.analyse(hid, vars.source, { currency: vars.currency, dateFormat: vars.dateFormat }),
    onSuccess: (data) => {
      setAnalysis(data);
      const chosenPlan = plans?.find((plan) => plan.id === planId);
      const nextCurrency = currency ?? data.currency.detected ?? chosenPlan?.currency ?? null;
      setCurrency(nextCurrency);
      setDateFormat((was) => was ?? data.date_format.detected);
      const eligible = new Set(
        data.targets.accounts.filter((target) => target.eligible).map((target) => target.id),
      );
      // Keep what the user already chose where it still makes sense; fill the
      // rest from the server's suggestions.
      setAccountMap((was) => {
        const next: Record<string, AccountChoice> = {};
        const taken = new Set<string>();
        for (const account of data.accounts) {
          let choice: AccountChoice | undefined = was[account.key];
          if (choice?.kind === "existing" && !eligible.has(choice.account_id)) choice = undefined;
          if (!choice) {
            choice = accountFromSuggestion(account.suggestion, account);
            if (choice.kind === "existing" && !eligible.has(choice.account_id))
              choice = { kind: "create", name: account.name, type: account.type_hint ?? "checking" };
          }
          // One to one, even when two suggestions name the same account.
          if (choice.kind === "existing") {
            if (taken.has(choice.account_id))
              choice = { kind: "create", name: account.name, type: account.type_hint ?? "checking" };
            else taken.add(choice.account_id);
          }
          next[account.key] = choice;
        }
        return next;
      });
      setCategoryMap((was) => {
        const next: Record<string, CategoryChoice> = {};
        for (const category of data.categories)
          next[category.key] = category.fixed_uncategorised
            ? { kind: "uncategorised" }
            : (was[category.key] ?? categoryFromSuggestion(category));
        return next;
      });
      forgetDryRun();
    },
  });

  function runAnalysis(overrides: { currency?: string | null; dateFormat?: string | null } = {}) {
    if (!source) return;
    analyseCall.mutate({
      source,
      currency: overrides.currency !== undefined ? overrides.currency : currency,
      dateFormat: overrides.dateFormat !== undefined ? overrides.dateFormat : dateFormat,
    });
  }

  function plan(): ImportPlan {
    const accounts: Record<string, AccountChoice> = {};
    const categories: Record<string, CategoryChoice> = {};
    for (const account of analysis?.accounts ?? [])
      accounts[account.key] = accountMap[account.key] ?? { kind: "skip" };
    for (const category of analysis?.categories ?? [])
      categories[category.key] = category.fixed_uncategorised
        ? { kind: "uncategorised" }
        : (categoryMap[category.key] ?? { kind: "uncategorised" });
    return {
      currency: currency ?? "",
      date_format: dateFormat,
      accounts,
      categories,
      flags,
      starting_balance: startingBalance,
      date_from: dateFrom || null,
      date_to: dateTo || null,
      acknowledge_cleared_reset: acknowledged,
      duplicates: { all: importAll ? "import" : null, import: importAll ? [] : [...importAnyway] },
    };
  }

  /**
   * A refusal of the whole plan, and the step where it can be put right.
   *
   * The server refuses a plan outright -- a new account's name is taken (409),
   * a target is in another currency, two YNAB accounts share one target --
   * and writes nothing. Showing that on the preview step would leave the user
   * reading an error about a choice two screens back, so they are taken to the
   * step that holds the choice, with the server's sentence as it was sent.
   */
  const [refusal, setRefusal] = useState<{ error: Error; step: Step } | null>(null);

  function refused(error: Error) {
    const status = (error as { status?: number }).status;
    if (status !== 409 && status !== 422) return;
    const text = error.message.toLowerCase();
    const to: Step | null = /currency code|date format/.test(text)
      ? "review"
      : /categor/.test(text)
        ? "categories"
        : status === 409 || /account|plan is in/.test(text)
          ? "accounts"
          : null;
    if (!to) return;
    forgetDryRun();
    setRefusal({ error, step: to });
    setStep(to);
  }

  const previewCall = useMutation({
    gcTime: 0,
    mutationFn: (vars: { source: Source; plan: ImportPlan }) =>
      calls.preview(hid, vars.source, vars.plan),
    onMutate: () => setRefusal(null),
    onError: refused,
    onSuccess: (data) => {
      setDryRun(data);
      setImportAll(false);
      setImportAnyway(new Set());
      setStep("preview");
    },
  });

  const commitCall = useMutation({
    gcTime: 0,
    mutationFn: (vars: { source: Source; plan: ImportPlan }) =>
      calls.commit(hid, vars.source, vars.plan),
    onMutate: () => setRefusal(null),
    onError: refused,
    onSuccess: (data) => {
      setReport(data);
      // Finished with: the key is not kept a moment longer than the import.
      setToken("");
      setStep("report");
      void client.invalidateQueries();
    },
  });

  const busy =
    plansCall.isPending || analyseCall.isPending || previewCall.isPending || commitCall.isPending;

  // ---- what Next means on each step, and whether it is allowed ---------- //

  const mappingsComplete =
    !!analysis &&
    analysis.accounts.every((account) => {
      const choice = accountMap[account.key];
      return !!choice && (choice.kind !== "create" || choice.name.trim() !== "");
    }) &&
    analysis.categories.every((category) => {
      if (category.fixed_uncategorised) return true;
      const choice = categoryMap[category.key];
      return !!choice && (choice.kind !== "create" || choice.name.trim() !== "");
    });

  const dateFormatNeeded = !!analysis?.date_format.ambiguous;
  const reviewReady =
    !!analysis && !!currency && (!dateFormatNeeded || !!dateFormat) && !analyseCall.isPending;

  const canNext: Record<Step, boolean> = {
    source: true,
    connect: via === "csv" ? !!file : token.trim() !== "",
    plan: !!planId,
    review: reviewReady,
    accounts: mappingsComplete,
    categories: mappingsComplete,
    options: acknowledged && !!source,
    preview: !!dryRun && !!source,
    report: false,
  };

  function next() {
    if (busy || !canNext[step]) return;
    switch (step) {
      case "connect":
        if (via === "api") {
          plansCall.mutate(token.trim());
          return;
        }
        setStep("review");
        if (!analysis) runAnalysis();
        return;
      case "plan":
        setStep("review");
        if (!analysis) runAnalysis();
        return;
      case "options":
        previewCall.mutate({ source: source!, plan: plan() });
        return;
      case "preview":
        commitCall.mutate({ source: source!, plan: plan() });
        return;
      default:
        setStep(steps[at + 1]);
    }
  }

  function back() {
    if (busy || at <= 0 || step === "report") return;
    if (step === "preview") forgetDryRun();
    setStep(steps[at - 1]);
  }

  const nextLabel =
    step === "options"
      ? t({ message: "Preview", comment: "Button on the one-time import: verb, see what the import would do without doing it" })
      : step === "preview"
        ? t({ message: "Import", comment: "Button on the one-time import: verb, run the import" })
        : step === "plan"
          ? t`Use this plan`
          : t({ message: "Next", comment: "Button on the one-time import: go to the next step" });

  const error =
    refusal && refusal.step === step
      ? null
      : step === "connect"
      ? plansCall.error
      : step === "review"
        ? analyseCall.error
        : step === "options"
          ? previewCall.error
          : step === "preview"
            ? commitCall.error
            : null;

  return (
    <div className="ynab-wizard">
      <nav className="ynab-stepper" aria-label={t({ message: "Steps", comment: "Screen-reader name on the one-time import" })}>
        {/* A phone gets one line and a bar instead of the row of circles; the
            list stays in the page for a screen reader either way. */}
        <div className="ynab-stepper-compact" aria-hidden="true">
          <span className="small">
            <Trans comment="Text on the one-time import">
              Step {at + 1} of {steps.length} — <strong>{STEP_LABELS[step]}</strong>
            </Trans>
          </span>
          <span className="ynab-stepper-bar">
            <span style={{ width: `${((at + 1) / steps.length) * 100}%` }} />
          </span>
        </div>
        <ol className="ynab-steps">
          {steps.map((one, index) => {
            const state = index < at ? "done" : index === at ? "current" : "todo";
            return (
              <li
                key={one}
                aria-current={state === "current" ? "step" : undefined}
                className={`ynab-step ${state}`}
              >
                <span className="ynab-step-number" aria-hidden="true">
                  {state === "done" ? "✓" : index + 1}
                </span>
                <span className="ynab-step-label">
                  <span className="sr-only">
                    {state === "done" ? t({ message: `Step ${index + 1}, done:`, comment: "Screen-reader text on the one-time import" }) : t({ message: `Step ${index + 1}:`, comment: "Screen-reader text on the one-time import" })}{" "}
                  </span>
                  {STEP_LABELS[one]}
                </span>
              </li>
            );
          })}
        </ol>
      </nav>

      <h3 className="section-title">
        {at + 1}. {STEP_LABELS[step]}
      </h3>

      {/* A refused plan is about a choice on this step: say so before the table. */}
      {refusal && refusal.step === step ? <Problem error={refusal.error} /> : null}

      {step === "source" && (
        <fieldset className="storage-choices" style={{ gridTemplateColumns: "1fr" }}>
          <legend className="sr-only"><Trans comment="Heading of a group of choices on the one-time import">Source app</Trans></legend>
          <label className="storage-choice chosen">
            <input type="radio" name="ynab-source-app" checked readOnly />
            <span>
              <strong>YNAB</strong>
              <span className="small muted">
                <Trans>
                  You Need A Budget: from its export file or its API.
                </Trans>
              </span>
            </span>
          </label>
        </fieldset>
      )}

      {step === "connect" && (
        <>
          <fieldset className="storage-choices">
            <legend className="sr-only"><Trans>How to connect</Trans></legend>
            <label className={via === "csv" ? "storage-choice chosen" : "storage-choice"}>
              <input
                type="radio"
                name="ynab-via"
                checked={via === "csv"}
                onChange={() => {
                  setVia("csv");
                  setToken("");
                  setPlans(null);
                  setPlanId("");
                  forgetAnalysis();
                }}
              />
              <span>
                <strong>
                  <Trans comment="Label of a choice on the one-time import">Export file</Trans>
                </strong>
                <span className="small muted">
                  <Trans>The .zip YNAB exports, or the CSV inside it.</Trans>
                </span>
              </span>
            </label>
            <label className={via === "api" ? "storage-choice chosen" : "storage-choice"}>
              <input
                type="radio"
                name="ynab-via"
                checked={via === "api"}
                onChange={() => {
                  setVia("api");
                  setFile(null);
                  forgetAnalysis();
                }}
              />
              <span>
                <strong>
                  <Trans comment="Label of a choice on the one-time import">API key</Trans>
                </strong>
                <span className="small muted">
                  <Trans>A personal access token from your YNAB account.</Trans>
                </span>
              </span>
            </label>
          </fieldset>

          {via === "csv" ? (
            <>
              <Field label={t({ message: "YNAB export", comment: "Label of a form field on the one-time import" })}>
                <input
                  type="file"
                  accept=".zip,.csv"
                  onChange={(event) => {
                    setFile(event.target.files?.[0] ?? null);
                    forgetAnalysis();
                    event.target.value = "";
                  }}
                />
              </Field>
              {file ? <p className="small muted">{file.name}</p> : null}
              <p className="small">
                <Trans>
                  You only need the *Register.csv file, not the *Plan.csv. The whole export .zip
                  works too.
                </Trans>
              </p>
            </>
          ) : (
            <>
              <Field label={t`YNAB personal access token`}>
                <input
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={token}
                  onChange={(event) => {
                    setToken(event.target.value);
                    setPlans(null);
                    setPlanId("");
                    forgetAnalysis();
                  }}
                />
              </Field>
              <p className="small">
                <Trans>
                  Make one in YNAB under{" "}
                  <a href={YNAB_DEVELOPER_URL} target="_blank" rel="noopener noreferrer">
                    Account settings → Developer settings
                  </a>
                  .
                </Trans>
              </p>
              <div className="banner info ynab-token-note" role="note">
                <strong>
                  <Trans>
                    Your key is used only for this import. It stays in this browser tab and is never
                    stored or logged by Spend Tracker.
                  </Trans>
                </strong>
              </div>
            </>
          )}
          {plansCall.isPending ? (
            <p className="small muted">
              <Trans>Asking YNAB for your plans…</Trans>
            </p>
          ) : null}
        </>
      )}

      {step === "plan" && (
        <>
          {plans && plans.length === 0 ? (
            <p className="small"><Trans>YNAB returned no plans for this key.</Trans></p>
          ) : (
            <fieldset className="storage-choices" style={{ gridTemplateColumns: "1fr" }}>
              <legend className="sr-only"><Trans>Which plan to import</Trans></legend>
              {(plans ?? []).map((one) => (
                <label
                  key={one.id}
                  className={planId === one.id ? "storage-choice chosen" : "storage-choice"}
                >
                  <input
                    type="radio"
                    name="ynab-plan"
                    checked={planId === one.id}
                    onChange={() => {
                      setPlanId(one.id);
                      forgetAnalysis();
                    }}
                  />
                  <span>
                    <strong>{one.name}</strong>
                    <span className="small muted">
                      {[
                        one.currency,
                        one.first_month || one.last_month
                          ? t({ message: `${one.first_month ?? "…"} to ${one.last_month ?? "…"}`, comment: "Label of a choice on the one-time import" })
                          : null,
                        one.last_modified_on ? t({ message: `last changed ${one.last_modified_on.slice(0, 10)}`, comment: "Label of a choice on the one-time import" }) : null,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  </span>
                </label>
              ))}
            </fieldset>
          )}
        </>
      )}

      {step === "review" && (
        <>
          {analyseCall.isPending ? (
            <p className="small muted">
              <Trans>Reading what YNAB holds…</Trans>
            </p>
          ) : null}
          {analysis ? (
            <Review
              analysis={analysis}
              via={via}
              currency={currency}
              dateFormat={dateFormat}
              baseCurrency={household.base_currency}
              busy={analyseCall.isPending}
              onCurrency={(value) => {
                setCurrency(value);
                runAnalysis({ currency: value });
              }}
              onDateFormat={(value) => {
                setDateFormat(value);
                runAnalysis({ dateFormat: value });
              }}
            />
          ) : null}
        </>
      )}

      {step === "accounts" && analysis && currency && (
        <MapAccounts
          accounts={analysis.accounts}
          targets={analysis.targets.accounts}
          currency={currency}
          choices={accountMap}
          onChoose={(key, choice) => setAccountMap((was) => ({ ...was, [key]: choice }))}
        />
      )}

      {step === "categories" && analysis && (
        <MapCategories
          categories={analysis.categories}
          targets={analysis.targets.categories}
          choices={categoryMap}
          onChoose={(key, choice) => setCategoryMap((was) => ({ ...was, [key]: choice }))}
        />
      )}

      {step === "options" && analysis && (
        <>
          <h4 className="ynab-sub" id="ynab-flags">
            <Trans comment="Heading on the one-time import: noun, YNAB's coloured markers on rows">
              Flags
            </Trans>
          </h4>
          <fieldset className="storage-choices" aria-labelledby="ynab-flags">
            <label className={flags === "memo" ? "storage-choice chosen" : "storage-choice"}>
              <input
                type="radio"
                name="ynab-flags"
                checked={flags === "memo"}
                onChange={() => setFlags("memo")}
              />
              <span>
                <strong>{t`Append to memo as 'Flag: <name>'`}</strong>
                <span className="small muted">
                  {analysis.flags.length
                    ? analysis.flags.map((one) => `${one.label} (${one.count})`).join(", ")
                    : t`No flagged rows were found.`}
                </span>
              </span>
            </label>
            <label className={flags === "ignore" ? "storage-choice chosen" : "storage-choice"}>
              <input
                type="radio"
                name="ynab-flags"
                checked={flags === "ignore"}
                onChange={() => setFlags("ignore")}
              />
              <span>
                <strong>
                  <Trans comment="Label of a choice on the one-time import">Ignore</Trans>
                </strong>
                <span className="small muted">
                  <Trans>Flags are left behind.</Trans>
                </span>
              </span>
            </label>
          </fieldset>

          <h4 className="ynab-sub"><Trans comment="Heading on the one-time import">Date range</Trans></h4>
          <p className="small muted" style={{ marginTop: 0 }}>
            {t`Leave both empty to import everything (${range(analysis.totals.date_min, analysis.totals.date_max)}).`}
          </p>
          <div className="row">
            <Field label={t({ message: "From", comment: "Label of a form field on the one-time import: the start of a range, or where money comes from" })}>
              <input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
            </Field>
            <Field label={t({ message: "To", comment: "Label of a form field on the one-time import: the end of a range, or where money goes" })}>
              <input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
            </Field>
          </div>

          <h4 className="ynab-sub" id="ynab-starting">
            {t`YNAB Starting Balance rows (${formatCount(analysis.totals.starting_balance_rows)})`}
          </h4>
          <fieldset className="storage-choices" aria-labelledby="ynab-starting">
            <label className={startingBalance === "import" ? "storage-choice chosen" : "storage-choice"}>
              <input
                type="radio"
                name="ynab-starting"
                checked={startingBalance === "import"}
                onChange={() => setStartingBalance("import")}
              />
              <span>
                <strong>{t({ message: "Import", context: "starting balance rows", comment: "Button on the one-time import: verb, run the import" })}</strong>
                <span className="small muted">
                  <Trans>As ordinary transactions.</Trans>
                </span>
              </span>
            </label>
            <label className={startingBalance === "skip" ? "storage-choice chosen" : "storage-choice"}>
              <input
                type="radio"
                name="ynab-starting"
                checked={startingBalance === "skip"}
                onChange={() => setStartingBalance("skip")}
              />
              <span>
                <strong>{t({ message: "Skip", context: "starting balance rows", comment: "Label of a choice on the one-time import (starting balance rows)" })}</strong>
                <span className="small muted">
                  <Trans>Leave them out; they are listed in the report.</Trans>
                </span>
              </span>
            </label>
          </fieldset>

          <label className="check ynab-ack">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            <Trans>
              I understand YNAB's Reconciled, Cleared and Uncleared states will be reset — everything
              arrives uncleared.
            </Trans>
          </label>
          {previewCall.isPending ? (
            <p className="small muted"><Trans>Running the import without keeping it…</Trans></p>
          ) : null}
        </>
      )}

      {step === "preview" && dryRun && (
        <>
          <p className="small">
            <Trans>
              Nothing has been imported yet. This is what the import will do, worked out by running it
              and throwing the result away.
            </Trans>
          </p>
          <Counts report={dryRun} />
          {dryRun.duplicates.length > 0 ? (
            <div style={{ marginTop: 18 }}>
              <Duplicates
                duplicates={dryRun.duplicates}
                currency={currency ?? household.base_currency}
                importAll={importAll}
                chosen={importAnyway}
                onImportAll={setImportAll}
                onToggle={(rowRef, importIt) =>
                  setImportAnyway((was) => {
                    const next = new Set(was);
                    if (importIt) next.add(rowRef);
                    else next.delete(rowRef);
                    return next;
                  })
                }
              />
            </div>
          ) : (
            <p className="small muted"><Trans>No duplicates of transactions already in the ledger.</Trans></p>
          )}
          {dryRun.not_imported.length > 0 ? (
            <details className="ynab-not-imported">
              <summary>
                {t`${formatCount(dryRun.not_imported.length)} will not be imported, with the reason for each`}
              </summary>
              <NotImportedTable
                rows={dryRun.not_imported}
                currency={currency ?? household.base_currency}
              />
            </details>
          ) : null}
          <UnpairedTransfers
            rows={dryRun.unpaired_transfers ?? []}
            currency={currency ?? household.base_currency}
            committed={false}
          />
          {commitCall.isPending ? (
            <p className="small muted">
              <Trans comment="Sentence on the one-time import">Importing…</Trans>
            </p>
          ) : null}
        </>
      )}

      {step === "report" && report && (
        <Report report={report} currency={currency ?? household.base_currency} householdId={household.id} />
      )}

      <Problem error={error} />

      <div className="ynab-nav">
        {step === "report" ? (
          <button type="button" className="primary" onClick={onClose}>
            <Trans comment="Button on the one-time import: finish and close">
              Done
            </Trans>
          </button>
        ) : (
          <>
            <button type="button" onClick={back} disabled={at === 0 || busy}>
              <Trans comment="Button on the one-time import: go to the previous step">
                Back
              </Trans>
            </button>
            <button
              type="button"
              className="primary"
              onClick={next}
              disabled={!canNext[step] || busy}
            >
              {nextLabel}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Step 4: what was found
// --------------------------------------------------------------------------- //

function Review({
  analysis,
  via,
  currency,
  dateFormat,
  baseCurrency,
  busy,
  onCurrency,
  onDateFormat,
}: {
  analysis: Analysis;
  via: Via;
  currency: string | null;
  dateFormat: string | null;
  baseCurrency: string;
  busy: boolean;
  onCurrency: (value: string) => void;
  onDateFormat: (value: string) => void;
}) {
  const totals = analysis.totals;
  const currencies = useMemo(() => {
    const all = new Set<string>();
    for (const code of [
      analysis.currency.detected,
      currency,
      baseCurrency,
      ...analysis.targets.accounts.map((target) => target.currency),
      ...COMMON_CURRENCIES,
    ])
      if (code) all.add(code.toUpperCase());
    return [...all].sort();
  }, [analysis, currency, baseCurrency]);

  const showCurrency = via === "csv" || analysis.currency.confirmed_needed || !currency;
  const where = analysis.source.filename ?? analysis.source.plan_name;

  return (
    <>
      {analysis.previous_imports.length > 0 ? (
        <div className="banner warn" role="alert">
          <Trans>
            <strong>This household has been imported into from YNAB before.</strong> Running it
            again adds the same history twice unless the rows are recognised as duplicates.
            Earlier runs:
          </Trans>
          <ul className="plain-list" style={{ marginTop: 6 }}>
            {analysis.previous_imports.map((one) => (
              <li key={one.batch_id}>
                {one.at.slice(0, 16).replace("T", " ")} · {one.via === "api" ? "API" : "CSV"} ·{" "}
                {one.filename ?? one.plan_name ?? "—"} · {one.status}
              </li>
            ))}
          </ul>
          <Trans>If you mean to replace an earlier run, undo it from History first.</Trans>
        </div>
      ) : null}

      {where ? (
        <p className="small">
          <Trans comment="Sentence on the one-time import">
            From <strong>{where}</strong>.
          </Trans>
        </p>
      ) : null}

      <dl className="facts">
        <dt>
          <Trans comment="Name of a fact on the one-time import. See GLOSSARY.md">Transactions</Trans>
        </dt>
        <dd>{formatCount(totals.rows)}</dd>
        <dt><Trans comment="Name of a fact on the one-time import: noun, a date range">Dates</Trans></dt>
        <dd>{range(totals.date_min, totals.date_max)}</dd>
        <dt><Trans comment="Name of a fact on the one-time import: noun, bank or cash accounts. See GLOSSARY.md">Accounts</Trans></dt>
        <dd>{formatCount(analysis.accounts.length)}</dd>
        <dt><Trans comment="Name of a fact on the one-time import. See GLOSSARY.md">Categories</Trans></dt>
        <dd>{formatCount(analysis.categories.length)}</dd>
        <dt>
          <Trans comment="Name of a fact on the one-time import: noun, money moved between your own accounts. See GLOSSARY.md">Transfers</Trans>
        </dt>
        <dd>
          {t({ message: plural(totals.transfers, {
            one: `${formatCount(totals.transfers)} leg`,
            other: `${formatCount(totals.transfers)} legs`,
          }), comment: "Value on the one-time import: how many transfer rows (legs) the file holds. See GLOSSARY.md" })}
        </dd>
        <dt><Trans comment="Name of a fact on the one-time import: noun, the parts of split transactions">Split parts</Trans></dt>
        <dd>{t`${formatCount(totals.splits)}, each imported as its own transaction`}</dd>
        <dt><Trans>Starting Balance rows</Trans></dt>
        <dd>{formatCount(totals.starting_balance_rows)}</dd>
        <dt><Trans comment="Name of a fact on the one-time import">Cleared states</Trans></dt>
        <dd>
          {t`${formatCount(totals.cleared.reconciled)} reconciled · ${formatCount(totals.cleared.cleared)} cleared · ${formatCount(totals.cleared.uncleared)} uncleared`}
        </dd>
        <dt><Trans comment="Name of a fact on the one-time import: noun, YNAB's coloured markers on rows">Flags</Trans></dt>
        <dd>
          {analysis.flags.length === 0
            ? t({ message: "None", context: "flags", comment: "Value of a fact on the one-time import (flags)" })
            : analysis.flags.map((one) => `${one.label} (${formatCount(one.count)})`).join(", ")}
        </dd>
        <dt><Trans comment="Name of a fact on the one-time import: noun. See GLOSSARY.md">Currency</Trans></dt>
        <dd>
          {analysis.currency.detected
            ? `${analysis.currency.detected}${analysis.currency.symbol ? ` (${analysis.currency.symbol})` : ""}`
            : t({ message: "Not detected", comment: "Value of a fact on the one-time import" })}
        </dd>
      </dl>

      <div className="row" style={{ marginTop: 12 }}>
        {showCurrency ? (
          <Field label={t`Currency of the plan`}>
            <select
              value={currency ?? ""}
              disabled={busy}
              onChange={(event) => event.target.value && onCurrency(event.target.value)}
            >
              {!currency ? <option value="">{t({ message: "Choose…", comment: "Option in a dropdown on the one-time import" })}</option> : null}
              {currencies.map((code) => (
                <option key={code} value={code}>
                  {code}
                </option>
              ))}
            </select>
          </Field>
        ) : null}
        {analysis.date_format.ambiguous ? (
          <Field label={t`Date format in the file`}>
            <select
              value={dateFormat ?? ""}
              disabled={busy}
              onChange={(event) => event.target.value && onDateFormat(event.target.value)}
            >
              {!dateFormat ? <option value="">{t({ message: "Choose…", comment: "Option in a dropdown on the one-time import" })}</option> : null}
              {analysis.date_format.options.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </Field>
        ) : null}
      </div>
      {showCurrency ? (
        <p className="small muted">
          <Trans>
            A YNAB plan has one currency. Only accounts here in that currency can take its rows.
          </Trans>
        </p>
      ) : null}
    </>
  );
}
