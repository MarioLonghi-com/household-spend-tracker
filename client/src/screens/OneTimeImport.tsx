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
 * closes. Nothing here decides whether a row can be imported; the preview is
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
    if (inProgress.current && !window.confirm("Close the import? Everything chosen so far will be lost.")) return;
    inProgress.current = false;
    setOpen(false);
  };
  return (
    <section className="card" aria-labelledby="one-time-import-title">
      <h2 id="one-time-import-title" className="section-title">
        One-time Import
      </h2>
      <p className="small">
        Bring your history over from the app you used before, once: transactions, accounts,
        categories and transfers, mapped onto what this household already has. YNAB is the one
        workflow so far. Other budgeting and spend tracker app import workflows can be added to
        this, just suggest it via{" "}
        <a href={NEW_ISSUE_URL} target="_blank" rel="noopener noreferrer">
          GitHub
        </a>
        .
      </p>
      <button type="button" className="primary" onClick={() => setOpen(true)}>
        Start a one-time import
      </button>
      {open ? (
        <Panel title="One-time Import" onClose={dismiss} config wide>
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

const STEP_LABELS: Record<Step, string> = {
  source: "Source app",
  connect: "Connect",
  plan: "Plan",
  review: "Review",
  accounts: "Accounts",
  categories: "Categories",
  options: "Flags & options",
  preview: "Preview",
  report: "Report",
};

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
    mutationFn: (key: string) => calls.listPlans(hid, key),
    onSuccess: (data) => {
      setPlans(data.plans);
      if (!data.plans.some((plan) => plan.id === planId)) setPlanId("");
      setStep("plan");
    },
  });

  const analyseCall = useMutation({
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
      ? "Preview"
      : step === "preview"
        ? "Import"
        : step === "plan"
          ? "Use this plan"
          : "Next";

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
      <nav className="ynab-stepper" aria-label="Steps">
        {/* A phone gets one line and a bar instead of the row of circles; the
            list stays in the page for a screen reader either way. */}
        <div className="ynab-stepper-compact" aria-hidden="true">
          <span className="small">
            Step {at + 1} of {steps.length} — <strong>{STEP_LABELS[step]}</strong>
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
                    Step {index + 1}
                    {state === "done" ? ", done" : ""}:{" "}
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
          <legend className="sr-only">Source app</legend>
          <label className="storage-choice chosen">
            <input type="radio" name="ynab-source-app" checked readOnly />
            <span>
              <strong>YNAB</strong>
              <span className="small muted">
                You Need A Budget: from its export file or its API.
              </span>
            </span>
          </label>
        </fieldset>
      )}

      {step === "connect" && (
        <>
          <fieldset className="storage-choices">
            <legend className="sr-only">How to connect</legend>
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
                <strong>Export file</strong>
                <span className="small muted">The .zip YNAB exports, or the CSV inside it.</span>
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
                <strong>API key</strong>
                <span className="small muted">A personal access token from your YNAB account.</span>
              </span>
            </label>
          </fieldset>

          {via === "csv" ? (
            <>
              <Field label="YNAB export">
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
                You only need the *Register.csv file, not the *Plan.csv. The whole export .zip
                works too.
              </p>
            </>
          ) : (
            <>
              <Field label="YNAB personal access token">
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
                Make one in YNAB under{" "}
                <a href={YNAB_DEVELOPER_URL} target="_blank" rel="noopener noreferrer">
                  Account settings → Developer settings
                </a>
                .
              </p>
              <div className="banner info ynab-token-note" role="note">
                <strong>
                  Your key is used only for this import. It stays in this browser tab and is never
                  stored or logged by Spend Tracker.
                </strong>
              </div>
            </>
          )}
          {plansCall.isPending ? <p className="small muted">Asking YNAB for your plans…</p> : null}
        </>
      )}

      {step === "plan" && (
        <>
          {plans && plans.length === 0 ? (
            <p className="small">YNAB returned no plans for this key.</p>
          ) : (
            <fieldset className="storage-choices" style={{ gridTemplateColumns: "1fr" }}>
              <legend className="sr-only">Which plan to import</legend>
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
                          ? `${one.first_month ?? "…"} to ${one.last_month ?? "…"}`
                          : null,
                        one.last_modified_on ? `last changed ${one.last_modified_on.slice(0, 10)}` : null,
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
          {analyseCall.isPending ? <p className="small muted">Reading what YNAB holds…</p> : null}
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
            Flags
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
                <strong>Append to memo as 'Flag: &lt;name&gt;'</strong>
                <span className="small muted">
                  {analysis.flags.length
                    ? analysis.flags.map((one) => `${one.label} (${one.count})`).join(", ")
                    : "No flagged rows were found."}
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
                <strong>Ignore</strong>
                <span className="small muted">Flags are left behind.</span>
              </span>
            </label>
          </fieldset>

          <h4 className="ynab-sub">Date range</h4>
          <p className="small muted" style={{ marginTop: 0 }}>
            Leave both empty to import everything ({range(analysis.totals.date_min, analysis.totals.date_max)}).
          </p>
          <div className="row">
            <Field label="From">
              <input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
            </Field>
            <Field label="To">
              <input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
            </Field>
          </div>

          <h4 className="ynab-sub" id="ynab-starting">
            YNAB Starting Balance rows ({analysis.totals.starting_balance_rows.toLocaleString()})
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
                <strong>Import</strong>
                <span className="small muted">As ordinary transactions.</span>
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
                <strong>Skip</strong>
                <span className="small muted">Leave them out; they are listed in the report.</span>
              </span>
            </label>
          </fieldset>

          <label className="check ynab-ack">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            I understand YNAB's Reconciled, Cleared and Uncleared states will be reset — everything
            arrives uncleared.
          </label>
          {previewCall.isPending ? (
            <p className="small muted">Running the import without keeping it…</p>
          ) : null}
        </>
      )}

      {step === "preview" && dryRun && (
        <>
          <p className="small">
            Nothing has been imported yet. This is what the import will do, worked out by running it
            and throwing the result away.
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
            <p className="small muted">No duplicates of transactions already in the ledger.</p>
          )}
          {dryRun.not_imported.length > 0 ? (
            <details className="ynab-not-imported">
              <summary>
                {dryRun.not_imported.length.toLocaleString()} will not be imported, with the reason
                for each
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
          {commitCall.isPending ? <p className="small muted">Importing…</p> : null}
        </>
      )}

      {step === "report" && report && (
        <Report report={report} currency={currency ?? household.base_currency} householdId={household.id} />
      )}

      <Problem error={error} />

      <div className="ynab-nav">
        {step === "report" ? (
          <button type="button" className="primary" onClick={onClose}>
            Done
          </button>
        ) : (
          <>
            <button type="button" onClick={back} disabled={at === 0 || busy}>
              Back
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
          <strong>This household has been imported into from YNAB before.</strong> Running it again
          adds the same history twice unless the rows are recognised as duplicates. Earlier runs:
          <ul className="plain-list" style={{ marginTop: 6 }}>
            {analysis.previous_imports.map((one) => (
              <li key={one.batch_id}>
                {one.at.slice(0, 16).replace("T", " ")} · {one.via === "api" ? "API" : "CSV"} ·{" "}
                {one.filename ?? one.plan_name ?? "—"} · {one.status}
              </li>
            ))}
          </ul>
          If you mean to replace an earlier run, undo it from History first.
        </div>
      ) : null}

      {where ? (
        <p className="small">
          From <strong>{where}</strong>.
        </p>
      ) : null}

      <dl className="facts">
        <dt>Transactions</dt>
        <dd>{totals.rows.toLocaleString()}</dd>
        <dt>Dates</dt>
        <dd>{range(totals.date_min, totals.date_max)}</dd>
        <dt>Accounts</dt>
        <dd>{analysis.accounts.length.toLocaleString()}</dd>
        <dt>Categories</dt>
        <dd>{analysis.categories.length.toLocaleString()}</dd>
        <dt>Transfers</dt>
        <dd>{totals.transfers.toLocaleString()} legs</dd>
        <dt>Split parts</dt>
        <dd>{totals.splits.toLocaleString()}, each imported as its own transaction</dd>
        <dt>Starting Balance rows</dt>
        <dd>{totals.starting_balance_rows.toLocaleString()}</dd>
        <dt>Cleared states</dt>
        <dd>
          {totals.cleared.reconciled.toLocaleString()} reconciled ·{" "}
          {totals.cleared.cleared.toLocaleString()} cleared ·{" "}
          {totals.cleared.uncleared.toLocaleString()} uncleared
        </dd>
        <dt>Flags</dt>
        <dd>
          {analysis.flags.length === 0
            ? "None"
            : analysis.flags.map((one) => `${one.label} (${one.count.toLocaleString()})`).join(", ")}
        </dd>
        <dt>Currency</dt>
        <dd>
          {analysis.currency.detected
            ? `${analysis.currency.detected}${analysis.currency.symbol ? ` (${analysis.currency.symbol})` : ""}`
            : "Not detected"}
        </dd>
      </dl>

      <div className="row" style={{ marginTop: 12 }}>
        {showCurrency ? (
          <Field label="Currency of the plan">
            <select
              value={currency ?? ""}
              disabled={busy}
              onChange={(event) => event.target.value && onCurrency(event.target.value)}
            >
              {!currency ? <option value="">Choose…</option> : null}
              {currencies.map((code) => (
                <option key={code} value={code}>
                  {code}
                </option>
              ))}
            </select>
          </Field>
        ) : null}
        {analysis.date_format.ambiguous ? (
          <Field label="Date format in the file">
            <select
              value={dateFormat ?? ""}
              disabled={busy}
              onChange={(event) => event.target.value && onDateFormat(event.target.value)}
            >
              {!dateFormat ? <option value="">Choose…</option> : null}
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
          A YNAB plan has one currency. Only accounts here in that currency can take its rows.
        </p>
      ) : null}
    </>
  );
}
