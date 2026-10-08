/**
 * Your own authenticator in recovery mode (#287), above every screen, for
 * every member -- not only owners.
 *
 * The sign-in screen goes straight from the recovery code to a new
 * authenticator, and keeps the grant that pays for it in this tab
 * (`recoveryGrant.ts`) so that putting it off costs nothing. But a reload on
 * that screen landed the member in the shell, signed in, with nothing on any
 * screen saying a new authenticator was still owed: the owners' banner speaks
 * of other members, and only the profile asked `GET /me/authenticator` or
 * read the grant. So the grant went unused, signing out dropped it, and the
 * next sign-in cost the second recovery code it was kept to save.
 *
 * Shares the profile's query (`["authenticator"]`), so re-enrolling there
 * takes this away at once, and asks every minute like the owners' banner, so
 * the original key put back takes it away too.
 */

import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { heldGrant } from "../lib/recoveryGrant";
import type { AuthenticatorStatus, User } from "../lib/types";
import { RECOVERY_MODE_POLL_MS } from "./RecoveryModeBanner";
import { Trans, useLingui } from "@lingui/react/macro";

export function ReenrolmentDue({ user, onOpen }: { user: User; onOpen: () => void }) {
  const { t } = useLingui();
  const status = useQuery({
    queryKey: ["authenticator"],
    queryFn: () => api.get<AuthenticatorStatus>("/me/authenticator"),
    staleTime: RECOVERY_MODE_POLL_MS,
    refetchInterval: RECOVERY_MODE_POLL_MS,
  });
  if (status.data?.locked_by_key !== true) return null;
  const covered = heldGrant(user.id) !== null;
  return (
    <div className="banner warn" role="status" aria-label={t({ message: "Your authenticator", comment: "Screen-reader name, ReenrolmentDue (shared)" })}>
      <Trans>
        This server's secret key was replaced, so your authenticator no longer works here, and
        until you set up a new one every sign-in will ask for another recovery code.
      </Trans>{" "}
      {covered ? (
        <Trans>
          The recovery code you signed in with in this tab covers it, while you stay signed in and
          for up to a day.
        </Trans>
      ) : (
        <Trans>Setting it up takes one of your recovery codes.</Trans>
      )}{" "}
      <button className="link" onClick={onOpen}>
        <Trans>Set up a new authenticator</Trans>
      </button>
    </div>
  );
}
