/**
 * How an accounts filter gathers its options -- the register's and the
 * income-and-expense report's, one copy (#110). There were two, kept equal by
 * a comment asking for this move.
 *
 * Country first, then type, because that is the order the question is usually
 * asked in: "what did we spend in Spain" comes up more than "what did the
 * savings accounts do". Both groupings list every account, so an account with
 * no country still appears -- under a heading that says so rather than being
 * quietly absent from a filter that claims to list accounts.
 */

import type { Account } from "../lib/types";
import type { PickerGroup } from "./GroupedPicker";

/** How an account type reads in a filter heading. */
export const TYPE_NAMES: Record<string, string> = {
  checking: "Checking",
  savings: "Savings",
  cash: "Cash",
  credit_card: "Credit cards",
  other_asset: "Other assets",
  other_liability: "Other liabilities",
};

export type AccountGrouping = "country" | "type";

export function accountGroups(accounts: Account[], grouping: AccountGrouping): PickerGroup[] {
  const buckets = new Map<string, { label: string; items: Account[] }>();
  for (const account of accounts) {
    const key = grouping === "country" ? (account.country ?? "—") : account.type;
    const label =
      grouping === "country"
        ? account.country
          ? `${account.flag} ${account.country}`
          : "No country set"
        : (TYPE_NAMES[account.type] ?? account.type);
    const bucket = buckets.get(key) ?? { label, items: [] };
    bucket.items.push(account);
    buckets.set(key, bucket);
  }
  return [...buckets.entries()]
    .sort((a, b) => a[1].label.localeCompare(b[1].label))
    .map(([key, bucket]) => ({
      key,
      label: bucket.label,
      items: bucket.items.map((account) => ({
        id: account.id,
        label: account.name,
        hint: account.currency,
      })),
    }));
}
