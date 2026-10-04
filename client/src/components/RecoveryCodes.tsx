/**
 * A fresh set of recovery codes, shown once, with the box the person has to
 * tick before they can move on.
 *
 * One component for every place codes are issued -- the setup wizard, an
 * accepted invitation, and making new ones from the profile screen (#289) --
 * so "I have stored these" means the same thing, and is asked the same way,
 * wherever it is asked. Only hashes are stored; this is the only time the
 * codes exist anywhere a person can read them.
 */

import { useState } from "react";

export function RecoveryCodeSheet({
  codes,
  action,
  busy = false,
  onStored,
}: {
  codes: string[];
  /** The button's words: what ticking the box lets them do next. */
  action: string;
  busy?: boolean;
  onStored: () => void;
}) {
  const [saved, setSaved] = useState(false);

  return (
    <>
      <p className="muted small">
        Each one works once, and they are shown only now. They are how you get back in if you
        lose your phone but still know your password.
      </p>
      <div className="codes">
        {codes.map((one) => (
          <span key={one}>{one}</span>
        ))}
      </div>
      <label className="small">
        <input
          type="checkbox"
          checked={saved}
          onChange={(e) => setSaved(e.target.checked)}
          style={{ width: "auto", marginRight: 8 }}
        />
        I have stored these somewhere that is not this browser
      </label>
      <p />
      <button className="primary" disabled={busy || !saved} onClick={onStored}>
        {action}
      </button>
    </>
  );
}
