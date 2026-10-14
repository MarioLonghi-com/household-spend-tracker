/**
 * Which backups in the backups folder to keep (#62).
 *
 * The proposal of 2026-10-13, with today added: **every backup of the newest
 * day, the newest of each of the last 30 days that have one, and the newest of
 * each of the last 12 months that have one.** "Today" is kept whole because a
 * person who pressed "Back up now" at noon expects that file to be there in
 * the evening, not replaced by the one made on quit.
 *
 * Everything else the app wrote there is removed after a new backup has been
 * written and checked, never before -- pruning first and then failing to write
 * turns one bad backup into none.
 *
 * "Days that have one", not calendar days: an app left closed for a month
 * still has thirty dailies to go back through when it is opened again.
 *
 * Only names this app writes are considered (`spendtracker-<UTC stamp>Z.sqlite3`).
 * The folder is the person's, and may be a synced one with their other files
 * in it; anything else there is never touched.
 */

export const DAILY = 30;
export const MONTHLY = 12;

/** `spendtracker-<UTC stamp>Z.sqlite3`, or `…Z-2.sqlite3` for a second one in the same second. */
const NAME = /^spendtracker-(\d{4})(\d{2})(\d{2})T(\d{6})Z(?:-(\d+))?\.sqlite3$/;

export function isOurs(name: string): boolean {
  return NAME.test(name);
}

/** Newest first: by the stamp, then by the same-second counter. */
export function newestFirst(names: readonly string[]): string[] {
  const key = (name: string) => {
    const [, year, month, day, time, again] = NAME.exec(name)!;
    return [`${year}${month}${day}${time}`, Number(again ?? 1)] as const;
  };
  return names.filter(isOurs).sort((a, b) => {
    const [stampA, againA] = key(a);
    const [stampB, againB] = key(b);
    return stampA === stampB ? againB - againA : stampA < stampB ? 1 : -1;
  });
}

/** The names to remove from `names`, which may hold anything. */
export function toRemove(names: readonly string[], daily = DAILY, monthly = MONTHLY): string[] {
  const ours = newestFirst(names);
  const keep = new Set<string>();
  const days = new Set<string>();
  const months = new Set<string>();
  let newestDay: string | null = null;
  for (const name of ours) {
    const [, year, month, day] = NAME.exec(name)!;
    const dayKey = `${year}-${month}-${day}`;
    const monthKey = `${year}-${month}`;
    newestDay ??= dayKey;
    if (dayKey === newestDay) keep.add(name);
    if (!days.has(dayKey) && days.size < daily) {
      days.add(dayKey);
      keep.add(name);
    }
    if (!months.has(monthKey) && months.size < monthly) {
      months.add(monthKey);
      keep.add(name);
    }
  }
  return ours.filter((name) => !keep.has(name));
}
