/** Payee naming rules: turning what the bank writes into what you call it. */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Empty, Field, Panel, Problem, SortHeading, sortRows, useSort } from "../components/bits";
import type {
  Household,
  Payee,
  PayeeRule,
  PayeeSuggestion,
  ReapplyPlan,
  ReapplyResult,
  RuleAction,
  RuleTrial,
} from "../lib/types";
import { t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { formatCount } from "../lib/locale";

type RuleSort = "pattern" | "match" | "payee" | "priority" | "enabled";

/**
 * The rules, and what the app thinks the next one should be.
 *
 * What each payee is then *categorised* as used to sit on this screen too, as
 * a table of every payee above the rules. It is its own screen now (#65): on
 * a real ledger that table is hundreds of rows, and it pushed the dozen rules
 * this screen is named after off the bottom of it.
 */
export function Rules({ household }: { household: Household }) {
  const client = useQueryClient();
  const [adding, setAdding] = useState(false);
  /** A suggestion the person accepted, carried into the new-rule panel. */
  const [seed, setSeed] = useState<{ pattern: string } | null>(null);
  const [tidying, setTidying] = useState(false);

  const rules = useQuery({
    queryKey: ["rules", household.id],
    queryFn: () => api.get<PayeeRule[]>(`/households/${household.id}/payee-rules`),
  });
  const payees = useQuery({
    queryKey: ["payees", household.id],
    queryFn: () => api.get<Payee[]>(`/households/${household.id}/payees`),
  });

  const refresh = () => {
    client.invalidateQueries({ queryKey: ["rules", household.id] });
    client.invalidateQueries({ queryKey: ["payees", household.id] });
    // Merging payees and re-categorising both move these counts about.
    client.invalidateQueries({ queryKey: ["payee-stats", household.id] });
    // And a re-apply is exactly the thing that makes a suggestion stale: the
    // group it was about has just become one payee, so leaving it on screen
    // offers a rule that would now do nothing.
    client.invalidateQueries({ queryKey: ["payee-suggestions", household.id] });
    client.invalidateQueries({ queryKey: ["reapply", household.id] });
  };

  const remove = useMutation({
    mutationFn: (id: string) => api.del(`/payee-rules/${id}`),
    onSuccess: refresh,
  });

  // Off, not gone: a rule that is wrong today may be right again after the bank
  // changes its wording, and deleting it loses the pattern somebody worked out.
  const setEnabled = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api.patch<PayeeRule>(`/payee-rules/${id}`, { enabled }),
    onSuccess: refresh,
  });

  const nameOf = (id: string | null) =>
    payees.data?.find((p) => p.id === id)?.name ?? "—";

  /** What the "Call it" column says for a rule. A rewrite rule has no payee
   *  to name -- that is the whole point of it -- so it says what it does to
   *  the string instead. Issue #58. */
  const becomes = (rule: PayeeRule) =>
    rule.action === "rewrite"
      ? rule.replacement
        ? `→ ${rule.replacement}`
        : t`whatever is left`
      : nameOf(rule.payee_id);

  // Priority ascending is the order the server sends, so the list opens on the
  // order the rules actually run in -- which is the one fact about a rule set
  // that changes what it does.
  const ruleOrder = useSort<RuleSort>("priority");
  const sortedRules = useMemo(
    () =>
      sortRows(
        rules.data ?? [],
        ruleOrder.sort,
        ruleOrder.direction,
        (rule, column) => {
          switch (column) {
            case "pattern":
              return rule.pattern;
            case "match":
              return rule.match_type;
            case "payee":
              return becomes(rule);
            case "enabled":
              return rule.enabled;
            default:
              return rule.priority;
          }
        },
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rules.data, payees.data, ruleOrder.sort, ruleOrder.direction],
  );

  return (
    <>
      {tidying && (
        <ReapplyPanel household={household} onClose={() => setTidying(false)} onDone={refresh} />
      )}
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 16 }}>
        <h1><Trans>Payee naming rules</Trans></h1>
        <button className="primary" onClick={() => setAdding(true)}>
          <Trans>
            Add a rule
          </Trans>
        </button>
      </div>
      <p className="muted small">
        <Trans>
          Applied as a statement is read, so <span className="mono">CARREFOUR MADRID 4432</span>{" "}
          arrives as Carrefour. The bank's own words are kept on the transaction either way.
        </Trans>
      </p>

      <Problem error={rules.error ?? remove.error ?? setEnabled.error} />

      <div className="card">
        {rules.data?.length === 0 ? (
          <Empty><Trans>No rules yet. Add one the next time a statement gives you a name you dislike.</Trans></Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <SortHeading
                    label={t`When the bank says`}
                    column="pattern"
                    sort={ruleOrder.sort}
                    direction={ruleOrder.direction}
                    onSort={ruleOrder.onSort}
                  />
                  <SortHeading
                    label={t({ message: "Match", comment: "Column heading on the Rules screen: how a rule compares the bank's text (contains, starts with…)" })}
                    column="match"
                    sort={ruleOrder.sort}
                    direction={ruleOrder.direction}
                    onSort={ruleOrder.onSort}
                  />
                  <SortHeading
                    label={t({ message: "Call it", comment: "Column heading on the Rules screen" })}
                    column="payee"
                    sort={ruleOrder.sort}
                    direction={ruleOrder.direction}
                    onSort={ruleOrder.onSort}
                  />
                  <SortHeading
                    label={t({ message: "Order", comment: "Column heading on the Rules screen: noun, sort order" })}
                    column="priority"
                    sort={ruleOrder.sort}
                    direction={ruleOrder.direction}
                    onSort={ruleOrder.onSort}
                  />
                  <SortHeading
                    label={t({ message: "On", comment: "Column heading on the Rules screen: switched on" })}
                    column="enabled"
                    sort={ruleOrder.sort}
                    direction={ruleOrder.direction}
                    onSort={ruleOrder.onSort}
                  />
                  <th />
                </tr>
              </thead>
              <tbody>
                {sortedRules.map((rule) => (
                  <tr key={rule.id}>
                    {/* The pattern is what a rule *is*; the payee it produces
                        is the figure on the right of the card. */}
                    <td className="mono small" data-primary="true">{rule.pattern}</td>
                    <td className="small muted" data-label={t({ message: "Match", comment: "Column heading on the Rules screen: how a rule compares the bank's text (contains, starts with…)" })} data-detail-first="true">
                      {rule.action === "rewrite"
                        ? t({ message: `${matchWord(rule.match_type)}, then strip`, comment: "Table cell on the Rules screen" })
                        : matchWord(rule.match_type)}
                    </td>
                    <td data-figure="true">{becomes(rule)}</td>
                    <td className="small muted" data-label={t({ message: "Priority", comment: "Column name shown beside a value on phones on the Rules screen: noun, which rule is tried first" })}>{rule.priority}</td>
                    <td>
                      <input
                        type="checkbox"
                        checked={rule.enabled}
                        aria-label={t`Apply the rule for ${rule.pattern}`}
                        onChange={(e) =>
                          setEnabled.mutate({ id: rule.id, enabled: e.target.checked })
                        }
                        style={{ width: "auto" }}
                      />
                    </td>
                    <td>
                      <button className="link danger" onClick={() => remove.mutate(rule.id)}>
                        <Trans comment="Button on the Rules screen: verb">
                          Delete
                        </Trans>
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* What the app could see and never said. Above the rules table,
          because it is the answer to "which rule should I write next" and
          that is the question somebody arrives at this screen holding. */}
      <Suggestions
        household={household}
        onAccept={(pattern) => {
          setSeed({ pattern });
          setAdding(true);
        }}
        onTidy={() => setTidying(true)}
      />

      {adding && (
        <RuleForm
          household={household}
          payees={payees.data ?? []}
          seed={seed}
          onClose={() => {
            setAdding(false);
            setSeed(null);
          }}
          onSaved={() => {
            setAdding(false);
            setSeed(null);
            refresh();
            // A new rule changes nothing already in the ledger on its own --
            // which is the whole of #60 -- so this is where the offer to
            // re-apply belongs: the moment somebody has just written one.
            setTidying(true);
          }}
        />
      )}
    </>
  );
}

function RuleForm({
  household,
  payees,
  onClose,
  onSaved,
  seed = null,
}: {
  household: Household;
  payees: Payee[];
  onClose: () => void;
  onSaved: () => void;
  /** A suggestion the person accepted, so the pattern arrives filled in. */
  seed?: { pattern: string } | null;
}) {
  const [matchType, setMatchType] = useState<PayeeRule["match_type"]>("contains");
  const [action, setAction] = useState<RuleAction>("map");
  const [pattern, setPattern] = useState(seed?.pattern ?? "");
  const [payeeName, setPayeeName] = useState("");
  const [replacement, setReplacement] = useState("");
  const [priority, setPriority] = useState(100);
  const rewriting = action === "rewrite";

  /**
   * What this pattern would claim, against the strings already in the ledger.
   *
   * The panel had no preview: a rule was written blind and the result
   * appeared at the next import. Run on demand rather than as you type --
   * a regex gets a 0.1s deadline per row, and firing that on every keystroke
   * would be the panel doing the thing the deadline exists to prevent.
   */
  const trial = useMutation({
    mutationFn: () =>
      api.post<RuleTrial>(`/households/${household.id}/payee-rules/try`, {
        match_type: matchType,
        action,
        pattern,
        replacement: rewriting ? replacement : null,
      }),
  });

  const save = useMutation({
    mutationFn: () =>
      api.post<PayeeRule>(`/households/${household.id}/payee-rules`, {
        match_type: matchType,
        action,
        pattern,
        // A rewrite rule has no payee, and sending an empty name would make
        // one called "" on the server. Sent as null rather than omitted so the
        // shape of the request does not depend on which branch wrote it.
        payee_name: rewriting ? null : payeeName,
        replacement: rewriting ? replacement : null,
        priority,
      }),
    onSuccess: onSaved,
  });

  return (
    <Panel title={t`New payee rule`} onClose={onClose} config>
      <Problem error={save.error ?? trial.error} />
      {/* What the panel never said, and every one of these cost somebody a
          confused ten minutes. The pattern is matched against the BANK's
          string, which is not the name in the register -- that is the whole
          reason a rule written against what is on screen matches nothing. */}
      <div className="banner info">
        <p className="small" style={{ marginTop: 0 }}>
          <Trans>
            A rule is matched against <strong>the bank&rsquo;s own words</strong>, not the payee
            you see in the register. The register may say <em>Amazon</em> while the statement
            said <span className="mono">COMPRA INTERNET WWW.AMAZON K513Z4FW5</span> &mdash; so
            write the rule for the second one. Open a transaction and press{" "}
            <em>Where did this come from?</em> to see exactly what arrived.
          </Trans>
        </p>
        <p className="small" style={{ marginBottom: 0 }}>
          <Trans>
            Case does not matter. <em>Matches the pattern</em> is a full regular expression,
            capped at 300 characters and given a tenth of a second per row &mdash; one that runs
            longer is set aside for the rest of that import. Try it below before you save it.
          </Trans>
        </p>
      </div>

      {/* The choice this panel could not previously offer, and the reason a
          rail needed a rule per shop. Issue #58. */}
      <Field label={t({ message: "And then", comment: "Label of a form field on the Rules screen" })}>
        <select value={action} onChange={(e) => setAction(e.target.value as RuleAction)}>
          <option value="map"><Trans>call it one payee</Trans></option>
          <option value="rewrite"><Trans>take this bit off, and see what is left</Trans></option>
        </select>
      </Field>
      <p />
      {rewriting ? (
        <div className="banner info">
          <p className="small" style={{ marginTop: 0 }}>
            <Trans>
              For a <strong>payment rail</strong> rather than a shop.{" "}
              <span className="mono">SQ *</span>, <span className="mono">PAGO MOVIL</span> and{" "}
              <span className="mono">COMPRA INTERNET</span> are how the money travelled, not who
              was paid &mdash; every shop that takes Square appears behind{" "}
              <span className="mono">SQ *</span>. There is no single payee to point at, so this
              kind of rule takes the rail off and lets whatever is left be the shop.
            </Trans>
          </p>
          <p className="small" style={{ marginBottom: 0 }}>
            <Trans>
              It runs <strong>before</strong> the rules that name a payee, and hands them what is
              left &mdash; so one <span className="mono">COMPRA INTERNET</span> rule plus your
              existing <em>Amazon</em> rule covers the whole Amazon-over-Santander family. Several
              of these compose: each one gets a turn, in order.
            </Trans>
          </p>
        </div>
      ) : (
        <p className="muted small">
          <Trans>
            A rule of this kind points at <strong>one</strong> payee, so{" "}
            <span className="mono">SQ *</span> cannot mean &ldquo;whatever comes after it&rdquo;
            &mdash; choose <em>take this bit off</em> for that.
          </Trans>
        </p>
      )}
      <p />
      <Field label={t`When the statement line`}>
        <select
          value={matchType}
          onChange={(e) => setMatchType(e.target.value as PayeeRule["match_type"])}
        >
          <option value="contains"><Trans comment="How a rule matches, inside the rule list: the bank's text contains the pattern">contains</Trans></option>
          <option value="prefix"><Trans comment="Option in a dropdown on the Rules screen: a rule's match: the text starts with this">starts with</Trans></option>
          <option value="equals"><Trans comment="Option in a dropdown on the Rules screen: a rule's match: the text is exactly this">is exactly</Trans></option>
          <option value="regex"><Trans>matches the pattern</Trans></option>
        </select>
      </Field>
      <p />
      <Field label={t({ message: "This text", comment: "Label of a form field on the Rules screen" })}>
        <input value={pattern} onChange={(e) => setPattern(e.target.value)} autoFocus />
      </Field>
      <p />
      {rewriting ? (
        <>
          <Field label={t`Put this back instead (leave empty to remove it)`}>
            <input
              value={replacement}
              onChange={(e) => setReplacement(e.target.value)}
              placeholder={t({ message: "usually nothing", comment: "Placeholder in an empty field on the Rules screen" })}
            />
          </Field>
          <p className="muted small">
            <Trans>
              Empty is the usual answer: <span className="mono">PAGO MOVIL BAR MARISOL</span>{" "}
              becomes <span className="mono">Bar Marisol</span>.
            </Trans>
            {matchType === "regex" ? (
              <>
                {" "}
                <Trans>
                  With <em>matches the pattern</em> this is a template, so{" "}
                  <span className="mono">\1</span> is the first bracketed group:{" "}
                  <span className="mono">^SQ \*(.+)$</span> with{" "}
                  <span className="mono">\1</span> keeps what follows.
                </Trans>
              </>
            ) : null}
          </p>
        </>
      ) : (
        <Field label={t`Call the payee`}>
          <input
            value={payeeName}
            onChange={(e) => setPayeeName(e.target.value)}
            list="rule-payees"
          />
          <datalist id="rule-payees">
            {payees.map((one) => (
              <option key={one.id} value={one.name} />
            ))}
          </datalist>
        </Field>
      )}
      <p />
      <Field label={t`Order (lower runs first)`}>
        <input
          type="number"
          value={priority}
          onChange={(e) => setPriority(Number(e.target.value))}
        />
      </Field>
      <p className="muted small">
        {rewriting
          ? t`Every rule of this kind gets a turn, in this order, each one working on what the last one left.`
          : t`The first rule that matches wins; the rest are not tried.`}
      </p>

      {/* The "test this rule" the screen never had. It is also what stops a
          regex that backtracks being discovered by an import going quiet. */}
      <div className="row" style={{ gap: 8 }}>
        <button disabled={!pattern.trim() || trial.isPending} onClick={() => trial.mutate()}>
          {trial.isPending ? t({ message: "Trying…", comment: "Button on the Rules screen" }) : t`What would this match?`}
        </button>
        <button
          className="primary"
          disabled={
            !pattern.trim() || (!rewriting && !payeeName.trim()) || save.isPending
          }
          onClick={() => save.mutate()}
        >
          <Trans comment="Button on the Rules screen: verb">
            Create
          </Trans>
        </button>
      </div>
      {trial.data ? (
        <div
          className={trial.data.timed_out ? "banner warn" : "banner info"}
          style={{ marginTop: 10 }}
        >
          {trial.data.timed_out ? (
            <p className="small" style={{ margin: 0 }}>
              <Trans>
                That pattern ran past its tenth of a second. An import would set it aside part
                way through and carry on without it, so it would work sometimes and not others.
                Simplify it.
              </Trans>
            </p>
          ) : (
            <>
              <p className="small" style={{ marginTop: 0 }}>
                {rewriting ? (
                  <Trans>
                    It rewrites <strong>{formatCount(trial.data.matches)}</strong> of{" "}
                    {formatCount(trial.data.considered)} rows already in this ledger.
                  </Trans>
                ) : (
                  <Trans>
                    It claims <strong>{formatCount(trial.data.matches)}</strong> of{" "}
                    {formatCount(trial.data.considered)} rows already in this ledger.
                  </Trans>
                )}
                {trial.data.matches === 0
                  ? ` ${t`Nothing — check you are matching what the bank wrote rather than what the register shows.`}`
                  : ""}
              </p>
              {trial.data.examples.length > 0 && (
                <ul className="plain-list mono small" style={{ marginBottom: 0 }}>
                  {trial.data.examples.map((one) => (
                    <li key={one}>{one}</li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      ) : null}
    </Panel>
  );
}

// --------------------------------------------------------------------------- //
// The groups that are one rule apart
// --------------------------------------------------------------------------- //

/**
 * Payees the app can already see are the same shop, and the person cannot.
 *
 * Four of the groups in the review are many-to-one -- every line is the same
 * merchant and the noise is a per-transaction reference -- and today's rule
 * engine handles every one of them. `contains WWW.AMAZON -> Amazon` is four
 * minutes' work.
 *
 * The gap was never capability. It is that the Payees screen lists payees and
 * the Rules screen lists rules, and neither looks at the raw strings the bank
 * sent -- although every row carries one, in `import_payee_original`, read by
 * nothing but the transaction panel. So four payees become twenty-eight and
 * the only way to notice is to scroll several hundred names.
 *
 * It compounds: reference numbers are unique per transaction, so this class of
 * payee grows one new row per purchase. Amazon alone will have hundreds within
 * a year.
 */
/** How the list names a rule's match, as the form's choices word it. */
function matchWord(type: string): string {
  switch (type) {
    case "contains":
      return t({ message: "contains", context: "rule list", comment: "How a rule matches, inside the rule list: the bank's text contains the pattern" });
    case "prefix":
      return t({ message: "prefix", context: "rule list", comment: "How a rule matches, inside the rule list: the bank's text starts with the pattern" });
    case "equals":
      return t({ message: "equals", context: "rule list", comment: "How a rule matches, inside the rule list: the bank's text is exactly the pattern" });
    case "regex":
      return t({ message: "regex", context: "rule list", comment: "How a rule matches, inside the rule list: the pattern is a regular expression" });
    default:
      return type;
  }
}

function Suggestions({
  household,
  onAccept,
  onTidy,
}: {
  household: Household;
  onAccept: (pattern: string) => void;
  onTidy: () => void;
}) {
  const found = useQuery({
    queryKey: ["payee-suggestions", household.id],
    queryFn: () =>
      api.get<PayeeSuggestion[]>(`/households/${household.id}/payee-suggestions`),
  });

  const suggestions = found.data ?? [];
  if (found.isLoading || suggestions.length === 0) return null;

  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 className="section-title"><Trans>These look like the same shop</Trans></h2>
        <button className="small-button" onClick={onTidy}>
          <Trans>
            Apply rules to what is already here
          </Trans>
        </button>
      </div>
      <p className="muted small" style={{ marginTop: 0 }}>
        <Trans>
          Grouped by what the bank sent, with the per-transaction reference taken off. Each one is
          a single rule away from being one payee.
        </Trans>
      </p>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th><Trans>The bank keeps saying</Trans></th>
              <th className="amount"><Trans comment="Column heading on the Rules screen">Spellings</Trans></th>
              <th className="amount"><Trans comment="Column heading on the Rules screen: noun, who was paid or who paid. See GLOSSARY.md">Payees</Trans></th>
              <th className="amount"><Trans comment="Column heading on the Rules screen: noun, lines of a file or table">Rows</Trans></th>
              <th />
            </tr>
          </thead>
          <tbody>
            {suggestions.map((one) => (
              <tr key={one.pattern}>
                <td data-primary="true">
                  <span className="mono">{one.pattern}</span>
                  <span className="small muted" style={{ display: "block" }}>
                    {one.examples.slice(0, 2).join(" · ")}
                  </span>
                </td>
                <td className="amount" data-label={t({ message: "Spellings", comment: "Column name shown beside a value on phones on the Rules screen" })}>
                  {one.strings}
                </td>
                {/* The number that makes the case. Four payees becoming one is
                    worth a click; one payee becoming one is not, and those are
                    filtered out server-side. */}
                <td className="amount" data-label={t({ message: "Payees", comment: "Column name shown beside a value on phones on the Rules screen: noun, who was paid or who paid. See GLOSSARY.md" })}>
                  {one.payees}
                </td>
                <td className="amount" data-label={t({ message: "Rows", comment: "Column name shown beside a value on phones on the Rules screen: noun, lines of a file or table" })}>
                  {one.transactions}
                </td>
                <td className="row-actions">
                  <button className="small-button" onClick={() => onAccept(one.pattern)}>
                    <Trans>
                      Write this rule
                    </Trans>
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Rules, applied to what is already here
// --------------------------------------------------------------------------- //

/**
 * Re-running the rules over rows that are already in the register.
 *
 * Rules ran in exactly one place -- `stage()`, as a statement was read -- and
 * the answer was frozen onto the line. Nothing consulted a rule again, so a
 * new rule left every row already in the ledger exactly as it was. And the
 * rows somebody is looking at when they decide to write a rule are, by
 * definition, already in the ledger.
 *
 * Previewed before it is applied, for the same reason the import is: this
 * changes hundreds of rows at once. One batch, so undo puts all of them back
 * together.
 */
function ReapplyPanel({
  household,
  onClose,
  onDone,
}: {
  household: Household;
  onClose: () => void;
  onDone: () => void;
}) {
  const [untouchedOnly, setUntouchedOnly] = useState(true);
  const [tidy, setTidy] = useState(true);

  const plan = useQuery({
    queryKey: ["reapply", household.id, untouchedOnly],
    queryFn: () =>
      api.post<ReapplyPlan>(`/households/${household.id}/payee-rules/reapply/preview`, {
        only_untouched: untouchedOnly,
      }),
  });

  const run = useMutation({
    mutationFn: () =>
      api.post<ReapplyResult>(
        `/households/${household.id}/payee-rules/reapply?delete_orphans=${tidy}`,
        { only_untouched: untouchedOnly },
      ),
    onSuccess: () => {
      onDone();
      onClose();
    },
  });

  const proposed = plan.data;
  /** Grouped by where the rows would land, which is how the question is asked. */
  const landing = useMemo(() => {
    const buckets = new Map<string, number>();
    for (const move of proposed?.moves ?? []) {
      buckets.set(move.to_name, (buckets.get(move.to_name) ?? 0) + 1);
    }
    return [...buckets.entries()].sort((a, b) => b[1] - a[1]);
  }, [proposed]);

  return (
    <Panel title={t`Apply rules to what is already here`} onClose={onClose} config>
      <Problem error={plan.error ?? run.error} />
      <p className="small muted" style={{ marginTop: 0 }}>
        <Trans>
          A rule only runs as a statement is read, so writing one changes nothing already in your
          register. This runs today&rsquo;s rules over the rows that are, matching against the
          bank&rsquo;s own words exactly as an import would.
        </Trans>
      </p>

      <label className="row" style={{ gap: 8, alignItems: "flex-start" }}>
        <input
          type="checkbox"
          checked={untouchedOnly}
          onChange={(e) => setUntouchedOnly(e.target.checked)}
          style={{ width: "auto", marginTop: 3 }}
        />
        <span>
          <strong>
            <Trans>Only rows nobody has corrected</Trans>
          </strong>
          <span className="small muted" style={{ display: "block" }}>
            <Trans>
              Leave a payee somebody set by hand alone. Turning this off lets the rules overrule
              those too, which is occasionally what you want and never what you want by accident.
            </Trans>
          </span>
        </span>
      </label>

      {plan.isLoading ? (
        <p className="muted small"><Trans>Working out what would change…</Trans></p>
      ) : proposed && proposed.changing === 0 ? (
        <div className="banner info" style={{ marginTop: 10 }}>
          <p className="small" style={{ margin: 0 }}>
            {t`Nothing would change. ${formatCount(proposed.considered)} rows carry the bank’s words and today’s rules already agree with all of them.`}
          </p>
        </div>
      ) : proposed ? (
        <>
          <div className="banner info" style={{ marginTop: 10 }}>
            <p className="small" style={{ marginTop: 0 }}>
              <Trans>
                <strong>{formatCount(proposed.changing)}</strong> of{" "}
                {formatCount(proposed.considered)} rows would move.
              </Trans>
            </p>
            <ul className="plain-list small" style={{ marginBottom: 0 }}>
              {landing.map(([name, count]) => (
                <li key={name}>
                  {formatCount(count)} &rarr; <strong>{name}</strong>
                </li>
              ))}
            </ul>
          </div>
          {proposed.orphaned.length > 0 && (
            <label className="row" style={{ gap: 8, alignItems: "flex-start", marginTop: 10 }}>
              <input
                type="checkbox"
                checked={tidy}
                onChange={(e) => setTidy(e.target.checked)}
                style={{ width: "auto", marginTop: 3 }}
              />
              <span>
                <strong>
                  {t`Also delete the ${formatCount(proposed.orphaned.length)} payees left empty`}
                </strong>
                <span className="small muted" style={{ display: "block" }}>
                  <Trans>
                    They were only ever the bank&rsquo;s reference numbers. Leaving them is what
                    makes a payee list that is still three hundred long afterwards.
                  </Trans>
                </span>
              </span>
            </label>
          )}
          <p className="muted small">
            <Trans>
              One act, so History has one entry and undo puts every row back together.
            </Trans>
          </p>
          <button className="primary" disabled={run.isPending} onClick={() => run.mutate()}>
            {run.isPending ? t({ message: "Applying…", comment: "Button on the Rules screen" }) : t({ message: `Move ${formatCount(proposed.changing)} rows`, comment: "Button on the Rules screen" })}
          </button>
        </>
      ) : null}
    </Panel>
  );
}
