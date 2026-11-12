/**
 * A passkey in place of the authenticator, for an account not yet written
 * (#456): the first-run setup, an invitation, a reset link.
 *
 * The server hands back creation options and a sealed blob that binds their
 * challenge to this wizard (`/setup/passkey/options`, `/invite/passkey/options`,
 * `/reset/{token}/passkey`). The browser's prompt runs, and the answer waits
 * in the page until the last step sends it -- where the server verifies it,
 * so nothing is stored for a passkey the server has not checked. Where
 * passkeys cannot work here, nothing is shown but the one line saying why,
 * and the authenticator stays the only choice.
 */

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { createPasskey, type PasskeyState, passkeyState, wasDismissed } from "../lib/passkeys";
import { whyNotHere } from "../screens/Passkeys";
import { Problem } from "./bits";
import { Trans } from "@lingui/react/macro";

/** A passkey the browser made, and the blob its challenge is bound to. */
export interface MadePasskey {
  blob: string;
  credential: unknown;
}

export function PasskeyChoice({
  offerPath,
  body,
  made,
  onMade,
  bare = false,
}: {
  /** Where to ask for creation options. */
  offerPath: string;
  body: unknown;
  made: MadePasskey | null;
  onMade: (made: MadePasskey) => void;
  /** The button and its outcome alone, for a screen that draws the choice
   *  itself: the first-run wizard's radio pair (#458). */
  bare?: boolean;
}) {
  // Asked once, here rather than through the query cache: these screens are
  // shown before anybody is signed in, outside the app's query provider.
  const [state, setState] = useState<PasskeyState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let gone = false;
    void passkeyState().then((found) => {
      if (!gone) setState(found);
    });
    return () => {
      gone = true;
    };
  }, []);

  if (!state) return null;
  if (!state.available) {
    return <p className="small muted">{whyNotHere(state)}</p>;
  }

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const offer = await api.post<{ blob: string; options: unknown }>(offerPath, body);
      const credential = await createPasskey(offer.options);
      onMade({ blob: offer.blob, credential });
    } catch (problem) {
      if (!wasDismissed(problem)) setError(problem);
    } finally {
      setBusy(false);
    }
  };

  const button = (
    <>
      <Problem error={error} />
      {made ? (
        <p className="small" role="status">
          <span aria-hidden="true">✓ </span>
          <Trans>Passkey saved on this device.</Trans>
        </p>
      ) : (
        <button className="primary" type="button" disabled={busy} onClick={create}>
          <span aria-hidden="true">🔑 </span>
          <Trans comment="Button on setup: make a passkey. See GLOSSARY.md">Create passkey</Trans>
        </button>
      )}
    </>
  );
  if (bare) return button;

  return (
    <div>
      <h3 className="section-title">
        <Trans comment="Heading of a choice on setup: the second step is a passkey. See GLOSSARY.md">Use a passkey</Trans>
      </h3>
      <p className="muted small">
        <Trans>Face ID, Touch ID, Windows Hello or a security key. Recommended.</Trans>
      </p>
      <Problem error={error} />
      {made ? (
        <p className="small" role="status">
          <Trans>Passkey saved on this device.</Trans>
        </p>
      ) : (
        <button className="primary" type="button" disabled={busy} onClick={create}>
          <Trans comment="Button on setup: make a passkey. See GLOSSARY.md">Create passkey</Trans>
        </button>
      )}
      <hr className="rule" />
      <h3 className="section-title">
        <Trans comment="Heading of a choice on setup: the second step is an authenticator app. See GLOSSARY.md">Use an authenticator app</Trans>
      </h3>
      <p className="muted small">
        <Trans>A six-digit code from an app on your phone.</Trans>
      </p>
    </div>
  );
}
