/**
 * Recovery mode, as the owners see it (#287).
 *
 * How many members' authenticators this server's `secret.key` cannot open --
 * most often because a new key was made at boot beside a ledger whose own key
 * went missing. Asked of the server, which asks the key; never stored, so the
 * banner goes within a minute of the original key being back or the last of
 * those members setting up a new authenticator, in whichever browser.
 *
 * Within a minute because it asks again every minute (`RECOVERY_MODE_POLL_MS`).
 * It sits above every screen and is never remounted, and the app does not
 * refetch on focus, so it once showed whatever the server said when the tab
 * opened until somebody reloaded it -- a member re-enrolling elsewhere, or the
 * key put back, changed nothing on an owner's open tab.
 *
 * It names the key where it came from. An instance keyed by
 * `SPENDTRACKER_SECRET_KEY` never reads `secret.key`, so naming the file sent
 * an owner to copy the right key into something the app ignores.
 *
 * Rendered for owners only: the route behind it answers 403 to a member.
 */

import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { RecoveryMode } from "../lib/types";

/** How often the banner asks the server again while it is on screen. */
export const RECOVERY_MODE_POLL_MS = 60_000;

export function RecoveryModeBanner() {
  const mode = useQuery({
    queryKey: ["recovery-mode"],
    queryFn: () => api.get<RecoveryMode>("/admin/recovery-mode"),
    staleTime: RECOVERY_MODE_POLL_MS,
    refetchInterval: RECOVERY_MODE_POLL_MS,
  });
  const locked = mode.data?.locked ?? 0;
  if (locked === 0) return null;
  const fromEnv = mode.data?.key_from_environment === true;
  return (
    <div className="banner warn" role="status">
      <span className="mono">{fromEnv ? "SPENDTRACKER_SECRET_KEY" : "secret.key"}</span> does not
      open {locked === 1 ? "1 member's authenticator" : `${locked} members' authenticators`}; they
      will be asked for a recovery code, then to set up a new authenticator.{" "}
      {fromEnv ? "Setting it back to the original key" : "Putting the original key back"} ends this
      for everyone who has not re-enrolled yet.
    </div>
  );
}
