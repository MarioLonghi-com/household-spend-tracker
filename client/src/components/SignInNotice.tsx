/**
 * The owner's notice of resets and new owners (#286).
 *
 * An owner can reset another owner's sign-in with no extra check -- decided in
 * #283, with this as the price: it cannot happen quietly. So every owner sees,
 * wherever they are in the app, what somebody *else* did in the last fourteen
 * days -- another owner, or a command on the server -- until they dismiss it.
 * Their own acts are on the Admin screen's list and need no interruption.
 *
 * "Wherever they are" includes a tab left open for a week: the notice sits in
 * the shell, outside the screens, so it mounts once per sign-in, and the app
 * does not refetch on focus by default. So it asks again on a timer, as
 * presence does, and whenever the tab comes back into view.
 *
 * The server reads the items from the audit log; nothing is stored for this.
 * Dismissal is the one thing kept, in this browser only, per viewer: a
 * convenience, so `readSticky` / `writeSticky`, which survive a private window
 * that refuses storage -- the notice then simply comes back next time. What
 * is kept is a position in the log *and* a time (`Dismissed`), so a restore
 * that rewinds the log cannot hide what happens after it.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { NOTHING_DISMISSED, dismissing, isDismissed, owedTo, sentence } from "../lib/signInChanges";
import { readSticky, writeSticky } from "../lib/sticky";
import type { SignInChange, User } from "../lib/types";

/** How many lines before "and N more". */
const SHOWN = 3;

/** How often a mounted notice asks again. Twice presence's interval: a
 * reset is rarer than a sign-in, and the query reads only the fortnight. */
export const NOTICE_REFRESH_MS = 60_000;

export function noticeKey(userId: string): string {
  return `spendtracker.shell.${userId}.signInNoticeSeen`;
}

export function SignInNotice({ user, onOpen }: { user: User; onOpen: () => void }) {
  const owner = user.role === "owner";
  // The Admin screen's own key, so a reset made there refreshes this too.
  const changes = useQuery({
    queryKey: ["admin", "sign-in-changes"],
    queryFn: () => api.get<SignInChange[]>("/admin/sign-in-changes"),
    enabled: owner,
    refetchInterval: NOTICE_REFRESH_MS,
    refetchOnWindowFocus: true,
  });
  const [seen, setSeen] = useState(
    () => readSticky(noticeKey(user.id), NOTHING_DISMISSED, isDismissed) ?? NOTHING_DISMISSED,
  );

  if (!owner) return null;
  const owed = owedTo(user.id, Array.isArray(changes.data) ? changes.data : [], seen);
  if (owed.length === 0) return null;

  const dismiss = () => {
    const upTo = dismissing(owed);
    writeSticky(noticeKey(user.id), upTo);
    setSeen(upTo);
  };

  return (
    <div className="banner warn" role="status" aria-label="Recent sign-in changes">
      <strong>Sign-in changes in the last 14 days, by somebody other than you:</strong>
      <ul style={{ margin: "6px 0", paddingLeft: 18 }}>
        {owed.slice(0, SHOWN).map((one) => (
          <li key={one.key}>{sentence(one)}.</li>
        ))}
        {owed.length > SHOWN && <li>and {owed.length - SHOWN} more.</li>}
      </ul>
      <div className="row" style={{ gap: 8 }}>
        <button onClick={onOpen}>See them in Admin</button>
        <button onClick={dismiss}>Dismiss</button>
      </div>
    </div>
  );
}
