/**
 * The category list, and what each payee does with it.
 *
 * Categories are classification, not a budget: no amounts, no targets, no
 * rollover. A category answers "what was this?" and nothing else, which is why
 * this screen is a list and not a spreadsheet.
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import {
  Dialog,
  Empty,
  Field,
  Hint,
  Panel,
  Problem,
  SortHeading,
  sortRows,
  useSort,
} from "../components/bits";
import type { Category, CategoryGroup, Household } from "../lib/types";
import { compareNames } from "../lib/locale";

/**
 * One line of a breakdown from `/stats/…`: what it is, and how many rows carry it.
 *
 * Declared here rather than in `lib/types.ts` because the two stat endpoints
 * are read by this screen and the payee categorisation screen and by nothing
 * else.
 * `key` is null for the unset bucket -- transactions with no payee, or none
 * with no category -- and the server names those too, so nothing here has to
 * invent a label for a null.
 */
export interface Tally {
  key: string | null;
  name: string;
  transaction_count: number;
}

/** `GET /households/{id}/stats/categories`, one entry per category in use. */
export interface CategoryStat {
  category_id: string;
  transaction_count: number;
  /** Distinct payees among them. "No payee" is not one of them. */
  payee_count: number;
  /** The busiest few, biggest first. May include a null-keyed "No payee". */
  payees: Tally[];
  /** Real payees the list left out, so "+N more" needs no arithmetic here. */
  more_payees: number;
}

interface CategoryStats {
  categories: CategoryStat[];
}

/**
 * `order` is the tree as the household arranged it, which is what this screen
 * opens on. The heading is on every group's card but the sort is one piece of
 * state: sorting by usage in one group sorts all of them, because the question
 * it answers -- "what is not being used?" -- is asked of the whole screen.
 *
 * `payees` sorts on how many different payees a category covers, which is the
 * meaning of that column rather than the string in it.
 */
type CategorySort = "order" | "name" | "usage" | "payees";

export function Categories({ household }: { household: Household }) {
  const client = useQueryClient();
  const [showArchived, setShowArchived] = useState(false);
  const [addingGroup, setAddingGroup] = useState(false);
  const [addingTo, setAddingTo] = useState<CategoryGroup | null>(null);
  const [editing, setEditing] = useState<Category | null>(null);
  const [editingGroup, setEditingGroup] = useState<CategoryGroup | null>(null);

  const tree = useQuery({
    queryKey: ["categories", household.id, showArchived],
    queryFn: () =>
      api.get<CategoryGroup[]>(
        `/households/${household.id}/categories${showArchived ? "?include_archived=true" : ""}`,
      ),
  });

  /**
   * The stat column, in one request for the whole screen.
   *
   * Not one request per category, and not a count worked out from rows this
   * screen does not have: the server groups `transactions` once and sends the
   * payee breakdown with it, so opening a category costs nothing further.
   */
  const stats = useQuery({
    queryKey: ["category-stats", household.id],
    queryFn: () =>
      api.get<CategoryStats>(`/households/${household.id}/stats/categories`),
  });
  const byCategory = useMemo(() => {
    const found = new Map<string, CategoryStat>();
    for (const one of stats.data?.categories ?? []) found.set(one.category_id, one);
    return found;
  }, [stats.data]);

  const refresh = () => {
    client.invalidateQueries({ queryKey: ["categories", household.id] });
    // Deleting a category changes what is behind the others, so the counts are
    // refetched with the tree rather than left showing the previous answer.
    client.invalidateQueries({ queryKey: ["category-stats", household.id] });
  };

  const seed = useMutation({
    mutationFn: () =>
      api.post<CategoryGroup[]>(`/households/${household.id}/categories/defaults`, {}),
    onSuccess: refresh,
  });

  const order = useSort<CategorySort>("order");
  const groups = useMemo(
    () =>
      (tree.data ?? []).map((group) => ({
        ...group,
        categories: sortRows(
          group.categories,
          order.sort,
          order.direction,
          (category, column) => {
            if (column === "name") return category.name;
            if (column === "usage") return category.used_by;
            // The meaning of the column, not the string in it: a category with
            // no transactions has no payees either, which is a 0 and not a gap.
            if (column === "payees") return byCategory.get(category.id)?.payee_count ?? 0;
            return null;
          },
          (a, b) => (order.sort === "order" ? 0 : compareNames(a.name, b.name)),
        ),
      })),
    [tree.data, byCategory, order.sort, order.direction],
  );
  const empty = groups.length === 0;

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 16 }}>
        <h1>Categories</h1>
        <label className="small muted" style={{ flex: "0 0 auto" }}>
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => setShowArchived(e.target.checked)}
            style={{ width: "auto", marginRight: 6 }}
          />
          Show archived
        </label>
        {!empty && (
          <button className="primary" onClick={() => setAddingGroup(true)}>
            Add a group
          </button>
        )}
      </div>

      <Problem error={tree.error ?? seed.error ?? stats.error} />

      {empty ? (
        <div className="card">
          <Empty>
            <p style={{ marginTop: 0 }}>
              No categories yet. Categories say what a transaction was <em>for</em> — they group
              the register and let a payee fill itself in.
            </p>
            <div className="row" style={{ justifyContent: "center", marginTop: 12 }}>
              <button
                className="primary"
                disabled={seed.isPending}
                onClick={() => seed.mutate()}
              >
                Start with a common set
              </button>
              <button onClick={() => setAddingGroup(true)}>Build my own</button>
            </div>
          </Empty>
        </div>
      ) : (
        groups.map((group) => (
          <div className="card" key={group.id}>
            <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
              {/* The heading is the way in to renaming or deleting the group
                  (#184), the same way a category's own row opens its panel. */}
              <h2 className="card-title">
                <button
                  className="link heading-link"
                  title="Rename or delete this group"
                  onClick={() => setEditingGroup(group)}
                >
                  {group.name}
                </button>
              </h2>
              <button className="link" onClick={() => setAddingTo(group)}>
                Add a category
              </button>
            </div>
            {group.categories.length === 0 ? (
              <p className="muted small" style={{ margin: 0 }}>
                Nothing in this group yet.
              </p>
            ) : (
              /* Each group is its own table, so with the default auto layout
                 every one of them sized its columns to its own longest string
                 -- "unused" in a group of unused categories, "1 transaction" in
                 the group next to it -- and the figures marched about from card
                 to card. Fixed widths, declared once, so the columns line up
                 down the whole screen. */
              /* Wrapped like every other table in the app: `.table-scroll` is
                 what the stylesheet keys the phone card layout off, and this
                 was the one screen whose tables were bare -- so it alone kept
                 a squeezed three-column table at 375px. */
              <div className="table-scroll">
              <table className="fixed-columns">
                <colgroup>
                  <col />
                  <col className="col-usage" />
                  <col className="col-payees" />
                  <col className="col-action" />
                </colgroup>
                <thead>
                  <tr>
                    <SortHeading
                      label="Category"
                      column="name"
                      sort={order.sort}
                      direction={order.direction}
                      onSort={order.onSort}
                    />
                    <SortHeading
                      label="Transactions"
                      column="usage"
                      sort={order.sort}
                      direction={order.direction}
                      onSort={order.onSort}
                      align="right"
                    />
                    <SortHeading
                      label="Payees"
                      column="payees"
                      sort={order.sort}
                      direction={order.direction}
                      onSort={order.onSort}
                      align="right"
                    />
                    <th className="amount" />
                  </tr>
                </thead>
                <tbody>
                  {group.categories.map((category) => (
                    <tr key={category.id}>
                      {/* The phone card's headline; see "Tables on a phone". */}
                      <td data-primary="true">
                        {category.name}
                        {category.archived ? (
                          <span className="tag closed" title="archived">
                            Archived
                          </span>
                        ) : null}
                      </td>
                      <td className="small muted amount" data-figure="true">
                        {/* The number that makes archive and delete different
                            choices rather than two words for one button. It
                            comes with the tree, so it is there before the stat
                            request lands and never disagrees with it -- both
                            are the same grouped count on the server. */}
                        {category.used_by === 0
                          ? "unused"
                          : category.used_by.toLocaleString()}
                      </td>
                      <td
                        className="small muted amount"
                        data-label="Payees"
                        data-detail-first="true"
                      >
                        {/* How many different payees are behind that figure.
                            One category of three hundred transactions from one
                            payee is a different thing from one of three hundred
                            from ninety, and the transaction count alone cannot
                            tell them apart. */}
                        {stats.isPending && category.used_by > 0
                          ? "…"
                          : (byCategory.get(category.id)?.payee_count ?? 0).toLocaleString()}
                      </td>
                      <td className="amount">
                        <button className="link" onClick={() => setEditing(category)}>
                          Edit
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            )}
          </div>
        ))
      )}

      {addingGroup && (
        <GroupForm
          household={household}
          onClose={() => setAddingGroup(false)}
          onSaved={() => {
            setAddingGroup(false);
            refresh();
          }}
        />
      )}
      {addingTo && (
        <CategoryForm
          household={household}
          group={addingTo}
          onClose={() => setAddingTo(null)}
          onSaved={() => {
            setAddingTo(null);
            refresh();
          }}
        />
      )}
      {editingGroup && (
        <GroupSettings
          group={editingGroup}
          onClose={() => setEditingGroup(null)}
          onSaved={() => {
            setEditingGroup(null);
            refresh();
          }}
        />
      )}
      {editing && (
        <CategorySettings
          category={editing}
          stat={byCategory.get(editing.id)}
          groups={groups}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
        />
      )}
    </>
  );
}

function GroupForm({
  household,
  onClose,
  onSaved,
}: {
  household: Household;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const save = useMutation({
    mutationFn: () => api.post(`/households/${household.id}/category-groups`, { name }),
    onSuccess: onSaved,
  });

  return (
    <Panel title="New group" onClose={onClose} config>
      <Problem error={save.error} />
      <Field label="Name">
        <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </Field>
      <p className="muted small">
        A heading the categories sit under — Bills, Everyday, whatever divides your spending in a
        way you recognise.
      </p>
      <button className="primary" disabled={!name.trim() || save.isPending} onClick={() => save.mutate()}>
        Create
      </button>
    </Panel>
  );
}

/**
 * Renaming a group, or deleting one that holds nothing (#184).
 *
 * Delete is always offered and the server decides. This screen cannot decide
 * it: with "Show archived" off, a group whose only categories are archived
 * looks empty here and is not. The refusal says why, in the alert at the top.
 *
 * A delete is a hard delete inside one batch, so it is an entry in History and
 * that entry's Undo is the way back -- there is no flag to untick.
 */
export function GroupSettings({
  group,
  onClose,
  onSaved,
}: {
  group: CategoryGroup;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(group.name);
  //: Delete asks first (#199); Undo in History is four steps away.
  const [confirming, setConfirming] = useState(false);
  const save = useMutation({
    mutationFn: () => api.patch(`/category-groups/${group.id}`, { name }),
    onSuccess: onSaved,
  });
  const remove = useMutation({
    mutationFn: () => api.del(`/category-groups/${group.id}`),
    onSuccess: onSaved,
  });

  return (
    <Panel title={group.name} onClose={onClose} config>
      {/* While the confirmation is open it shows the refusal itself. */}
      <Problem error={save.error ?? (confirming ? null : remove.error)} />
      <Field label="Name">
        <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </Field>
      <div className="row" style={{ marginTop: 16 }}>
        <button
          className="primary"
          disabled={!name.trim() || name.trim() === group.name || save.isPending}
          onClick={() => {
            remove.reset();
            save.mutate();
          }}
        >
          Save
        </button>
        <button
          className="danger"
          disabled={remove.isPending}
          onClick={() => {
            save.reset();
            remove.reset();
            setConfirming(true);
          }}
        >
          Delete group
        </button>
      </div>
      <p className="small muted" style={{ marginTop: 10 }}>
        A group can only be deleted when there are no categories under it, archived ones included.
        Deleting it is an entry in History, and Undo there brings it back.
      </p>
      {confirming && (
        <Dialog title={`Delete the group ${group.name}?`} onClose={() => setConfirming(false)}>
          <p style={{ marginTop: 0 }}>Undo in History brings it back.</p>
          <div className="dialog-choices">
            <button className="danger" disabled={remove.isPending} onClick={() => remove.mutate()}>
              {remove.isPending ? "Deleting…" : "Yes, delete it"}
            </button>
            <button disabled={remove.isPending} onClick={() => setConfirming(false)}>
              Keep it
            </button>
          </div>
          <Problem error={remove.error} />
        </Dialog>
      )}
    </Panel>
  );
}

function CategoryForm({
  household,
  group,
  onClose,
  onSaved,
}: {
  household: Household;
  group: CategoryGroup;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const save = useMutation({
    mutationFn: () =>
      api.post(`/households/${household.id}/categories`, { group_id: group.id, name }),
    onSuccess: onSaved,
  });

  return (
    <Panel title={`New category in ${group.name}`} onClose={onClose} config>
      <Problem error={save.error} />
      <Field label="Name">
        <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </Field>
      <button className="primary" disabled={!name.trim() || save.isPending} onClick={() => save.mutate()}>
        Create
      </button>
    </Panel>
  );
}

/**
 * Who is behind a category.
 *
 * The count on the row says how much is in here; this says what it is made of.
 * A short list, biggest first, because the question it answers -- "is this
 * category one shop or forty?" -- is answered by the top of the list, and the
 * bottom of a four-hundred-payee list is a different screen's job.
 */
function PayeeBreakdown({ stat }: { stat: CategoryStat | undefined }) {
  if (!stat || stat.payees.length === 0) return null;
  return (
    <>
      <h3 className="section-title">Payees</h3>
      <ul className="breakdown">
        {stat.payees.map((one) => (
          <li key={one.key ?? "none"}>
            <span className={one.key ? "" : "muted"}>{one.name}</span>
            <span className="small muted">{one.transaction_count.toLocaleString()}</span>
          </li>
        ))}
      </ul>
      {stat.more_payees > 0 && (
        <p className="muted small" style={{ marginTop: 6 }}>
          and {stat.more_payees.toLocaleString()} more{" "}
          {stat.more_payees === 1 ? "payee" : "payees"} — {stat.payee_count.toLocaleString()} in
          all.
        </p>
      )}
    </>
  );
}

export function CategorySettings({
  category,
  stat,
  groups,
  onClose,
  onSaved,
}: {
  category: Category;
  /** Absent for a category nothing is filed under; the server sends no row. */
  stat: CategoryStat | undefined;
  groups: CategoryGroup[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(category.name);
  const [groupId, setGroupId] = useState(category.group_id);
  const [archived, setArchived] = useState(category.archived);
  //: Delete asks first (#199); Undo in History is four steps away.
  const [confirming, setConfirming] = useState(false);

  const save = useMutation({
    mutationFn: () =>
      api.patch(`/categories/${category.id}`, { name, group_id: groupId, archived }),
    onSuccess: onSaved,
  });
  const remove = useMutation({
    mutationFn: () => api.del(`/categories/${category.id}`),
    onSuccess: onSaved,
  });

  return (
    <Panel title={category.name} onClose={onClose} config>
      {/* While the confirmation is open it shows the refusal itself. */}
      <Problem error={save.error ?? (confirming ? null : remove.error)} />
      <Field label="Name">
        <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </Field>
      <p />
      <Field label="Group">
        <select value={groupId} onChange={(e) => setGroupId(e.target.value)}>
          {groups.map((one) => (
            <option key={one.id} value={one.id}>
              {one.name}
            </option>
          ))}
        </select>
      </Field>
      <p />
      <label className="small">
        <input
          type="checkbox"
          checked={archived}
          onChange={(e) => setArchived(e.target.checked)}
          style={{ width: "auto", marginRight: 8 }}
        />
        Archived — hide it from the pickers
      </label>
      <p className="muted small">
        Archiving takes it out of the lists and leaves it on everything it ever categorised, so
        the past still reads correctly.
      </p>

      <div className="row" style={{ marginTop: 16 }}>
        <button className="primary" disabled={!name.trim() || save.isPending} onClick={() => save.mutate()}>
          Save
        </button>
        {/* Offered only when it would work. A delete button that always answers
            "archive it instead" is a button that does nothing. */}
        {category.used_by === 0 && (
          <button
            className="danger"
            disabled={remove.isPending}
            onClick={() => {
              remove.reset();
              setConfirming(true);
            }}
          >
            Delete
          </button>
        )}
      </div>
      {confirming && (
        <Dialog title={`Delete the category ${category.name}?`} onClose={() => setConfirming(false)}>
          <p style={{ marginTop: 0 }}>Undo in History brings it back.</p>
          <div className="dialog-choices">
            <button className="danger" disabled={remove.isPending} onClick={() => remove.mutate()}>
              {remove.isPending ? "Deleting…" : "Yes, delete it"}
            </button>
            <button disabled={remove.isPending} onClick={() => setConfirming(false)}>
              Keep it
            </button>
          </div>
          <Problem error={remove.error} />
        </Dialog>
      )}
      {category.used_by > 0 && (
        <p className="small muted" style={{ marginTop: 10 }}>
          {category.used_by} transaction{category.used_by === 1 ? " carries" : "s carry"} this
          category, so it cannot be deleted. Archive it instead.
        </p>
      )}

      {/* Which payees those transactions are, and how many each. Sent with the
          list the row came from, so opening this panel is not a request. */}
      <PayeeBreakdown stat={stat} />
    </Panel>
  );
}

/**
 * How one payee decides its category. The three modes from the design.
 *
 * Opened from the payee categorisation screen and from the register, because the moment
 * you want to change this is the moment you have just corrected a category by
 * hand and thought "again?".
 */
export function PayeeCategorisationPanel({
  payeeId,
  household,
  onClose,
}: {
  payeeId: string;
  household: Household;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const rule = useQuery({
    queryKey: ["payee-categorisation", payeeId],
    queryFn: () => api.get<import("../lib/types").PayeeCategorisation>(
      `/payees/${payeeId}/categorisation`,
    ),
  });
  const tree = useQuery({
    queryKey: ["categories", household.id, false],
    queryFn: () => api.get<CategoryGroup[]>(`/households/${household.id}/categories`),
  });

  const [mode, setMode] = useState<string | null>(null);
  const [categoryId, setCategoryId] = useState<string | null>(null);

  const chosenMode = mode ?? rule.data?.categorisation ?? "history";
  const chosenCategory = categoryId ?? rule.data?.default_category_id ?? "";

  const save = useMutation({
    mutationFn: () =>
      api.put(`/payees/${payeeId}/categorisation`, {
        categorisation: chosenMode,
        category_id: chosenCategory || null,
      }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["payee-categorisation", payeeId] });
      client.invalidateQueries({ queryKey: ["register", household.id] });
      onClose();
    },
  });

  const name = rule.data?.payee_name ?? "this payee";

  return (
    <Panel title={name} onClose={onClose} config>
      <Problem error={rule.error ?? tree.error ?? save.error} />

      <h3 className="section-title">
        Categorisation
        <Hint label="how this works">
          <p>
            Every new transaction for this payee gets a category without you choosing one. Which
            one depends on the setting below.
          </p>
          <p className="muted small" style={{ marginBottom: 0 }}>
            It matters most on an import: a statement of three hundred rows is three hundred
            categories you did not have to type, or three hundred corrections if it guesses badly.
          </p>
        </Hint>
      </h3>

      <fieldset className="modes">
        <legend className="sr-only">How to categorise {name}</legend>

        <label className="mode">
          <input
            type="radio"
            name="categorisation"
            checked={chosenMode === "history"}
            onChange={() => setMode("history")}
          />
          <span>
            <strong>Auto-categorise based on payee history</strong>
            <span className="small muted">
              The category is chosen from how the last {rule.data?.history_window ?? 3}{" "}
              transactions for {name} were categorised — the most common of them, most recent
              winning a tie. It updates as you add and edit transactions.
            </span>
            {rule.data?.current_default_name ? (
              <span className="small" style={{ marginTop: 6, display: "block" }}>
                Right now that is <strong>{rule.data.current_default_name}</strong>.
              </span>
            ) : (
              <span className="small muted" style={{ marginTop: 6, display: "block" }}>
                Nothing to go on yet, so new transactions stay uncategorised.
              </span>
            )}
          </span>
        </label>

        <label className="mode">
          <input
            type="radio"
            name="categorisation"
            checked={chosenMode === "fixed"}
            onChange={() => setMode("fixed")}
          />
          <span>
            <strong>Always use my choice</strong>
            <select
              value={chosenCategory}
              disabled={chosenMode !== "fixed"}
              onChange={(e) => setCategoryId(e.target.value)}
              style={{ marginTop: 6 }}
            >
              <option value="">Choose a category…</option>
              {(tree.data ?? []).map((group) => (
                <optgroup key={group.id} label={group.name}>
                  {group.categories.map((one) => (
                    <option key={one.id} value={one.id}>
                      {one.name}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </span>
        </label>

        <label className="mode">
          <input
            type="radio"
            name="categorisation"
            checked={chosenMode === "none"}
            onChange={() => setMode("none")}
          />
          <span>
            <strong>Do not auto-categorise {name}</strong>
            <span className="small muted">
              New transactions arrive blank and you choose each time. Right for a payee that is
              genuinely different every time.
            </span>
          </span>
        </label>
      </fieldset>

      <button
        className="primary"
        style={{ marginTop: 16 }}
        disabled={save.isPending || (chosenMode === "fixed" && !chosenCategory)}
        onClick={() => save.mutate()}
      >
        Save
      </button>
    </Panel>
  );
}
