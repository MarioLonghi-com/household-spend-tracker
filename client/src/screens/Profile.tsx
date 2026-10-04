/**
 * Your own account: the password, the second factor, and the keys you have
 * given to programs.
 *
 * The first two ask for the current password even though you are already
 * signed in. A session proves this browser was signed in at some point; it
 * does not prove who is at the keyboard, and these are exactly the changes
 * that would let somebody who found an unlocked laptop keep the account.
 *
 * Issuing a key asks for more than that — the password *and* a live
 * authenticator code — because it is the only act here whose effect outlives
 * the session that performed it. Changing a password signs out every other
 * browser and replacing an authenticator un-trusts every device, so both are
 * loud and their owner finds out. A key is quiet: it keeps working from
 * somewhere else for months, and nothing about this browser says so.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Empty, Field, Hint, Panel, Problem } from "../components/bits";
import { stepUpToken } from "../components/StepUp";
import { RecoveryCodeSheet } from "../components/RecoveryCodes";
import { formatInstant } from "../lib/time";
import { dropGrant, heldGrant } from "../lib/recoveryGrant";
import {
  type Appearance,
  chooseAppearance,
  effectiveScheme,
  storedAppearance,
} from "../lib/appearance";
import type { AgentKey, AuthenticatorStatus, Household, User } from "../lib/types";

type Offer = { token: string; secret: string; uri: string };

export function Profile({
  user,
  households,
  onClose,
}: {
  user: User;
  households: Household[];
  onClose: () => void;
}) {
  return (
    <Panel title="Your account" onClose={onClose} config>
      <p className="muted small" style={{ marginTop: 0 }}>
        Signed in as <strong>{user.display_name}</strong> ({user.email}).
      </p>
      <AppearanceSection />
      <hr className="rule" />
      <PasswordSection />
      <hr className="rule" />
      <AuthenticatorSection user={user} />
      <hr className="rule" />
      <RecoveryCodesSection />
      <hr className="rule" />
      <KeysSection households={households} />
    </Panel>
  );
}


/**
 * Light, dark, or follow the machine.
 *
 * First in the panel and not last, because it is the only thing here that is
 * not about credentials -- and because it is the one somebody will come looking
 * for. The other three sections all end in "and now every other browser is
 * signed out"; this one ends in the page changing colour, which is why it also
 * takes effect on the click rather than behind a Save.
 *
 * Per device rather than per account: the phone in a dark kitchen and the
 * desktop at a bright window are different answers to the same question. It is
 * not sent to the server and there is no column for it.
 */
function AppearanceSection() {
  const [choice, setChoice] = useState<Appearance>(storedAppearance);

  const pick = (next: Appearance) => {
    setChoice(next);
    chooseAppearance(next);
  };

  const options: { key: Appearance; label: string; icon: string }[] = [
    { key: "light", label: "Light", icon: "\u2600\ufe0f" },
    { key: "dark", label: "Dark", icon: "\ud83c\udf19" },
    { key: "system", label: "System", icon: "\ud83d\udcbb" },
  ];

  return (
    <section>
      <h3>
        Appearance{" "}
        <Hint label="about appearance">
          Kept on this device only, so a phone and a laptop can differ. Nothing
          is sent to the server, and clearing this browser's site data forgets
          it -- which returns you to following the system.
        </Hint>
      </h3>

      <div className="appearance-choices" role="group" aria-label="Appearance">
        {options.map((one) => (
          <button
            key={one.key}
            type="button"
            className={one.key === choice ? "appearance chosen" : "appearance"}
            aria-pressed={one.key === choice}
            onClick={() => pick(one.key)}
          >
            <span aria-hidden="true">{one.icon}</span> {one.label}
          </button>
        ))}
      </div>

      <p className="muted small">
        {choice === "system"
          ? `Following this device, which is ${effectiveScheme("system")} right now.`
          : `Held in ${choice}, whatever this device is set to.`}
      </p>
    </section>
  );
}

function PasswordSection() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");

  const change = useMutation({
    mutationFn: () =>
      api.post<{ other_sessions_ended: number; devices_revoked: number; keys_still_live: number }>(
        "/me/password",
        {
          current_password: current,
          new_password: next,
        },
      ),
    onSuccess: () => {
      setCurrent("");
      setNext("");
      setAgain("");
    },
  });

  // Checked here as well as on the server: the typist should find out before
  // the round trip, and the server should never trust that they did.
  const mismatch = again.length > 0 && next !== again;
  const ready = current.length > 0 && next.length > 0 && !mismatch;

  return (
    <section>
      <h3 className="section-title">Password</h3>
      <Problem error={change.error} />
      {change.isSuccess && (
        <div className="banner info">
          Password changed.{" "}
          {change.data.other_sessions_ended > 0
            ? `${change.data.other_sessions_ended} other ${
                change.data.other_sessions_ended === 1 ? "browser was" : "browsers were"
              } signed out.`
            : "No other browsers were signed in."}
          {change.data.devices_revoked > 0
            ? ` ${change.data.devices_revoked} trusted ${
                change.data.devices_revoked === 1 ? "browser" : "browsers"
              } will ask for a code again.`
            : ""}
          {change.data.keys_still_live > 0
            ? ` ${change.data.keys_still_live} agent ${
                change.data.keys_still_live === 1 ? "key still works" : "keys still work"
              } — revoke ${change.data.keys_still_live === 1 ? "it" : "them"} below if ${
                change.data.keys_still_live === 1 ? "it" : "they"
              } should stop.`
            : ""}
        </div>
      )}

      <Field label="Current password">
        <input
          type="password"
          value={current}
          autoComplete="current-password"
          onChange={(e) => setCurrent(e.target.value)}
        />
      </Field>
      <p />
      <Field
        label="New password"
        hint={
          <Hint label="what makes a good one">
            <p>
              Length does more than symbols do. Four unrelated words you can actually remember
              beats a short one with punctuation in it.
            </p>
            <p className="muted small" style={{ marginBottom: 0 }}>
              Changing it signs out every other browser and forgets every trusted one, so if
              you are changing it because somebody else knows it, they are out too. Agent keys
              are separate credentials and keep working until you revoke them.
            </p>
          </Hint>
        }
      >
        <input
          type="password"
          value={next}
          autoComplete="new-password"
          onChange={(e) => setNext(e.target.value)}
        />
      </Field>
      <p />
      <Field label="New password again">
        <input
          type="password"
          value={again}
          autoComplete="new-password"
          onChange={(e) => setAgain(e.target.value)}
        />
      </Field>
      {mismatch ? <p className="small neg">Those two don't match.</p> : null}

      <button
        className="primary"
        style={{ marginTop: 12 }}
        disabled={!ready || change.isPending}
        onClick={() => change.mutate()}
      >
        Change password
      </button>
    </section>
  );
}

/**
 * A new authenticator, proved by the old one -- or, in recovery mode (#287),
 * where the server's key cannot open the old one, by a recovery code or by
 * the grant the recovery sign-in left in this tab.
 *
 * The grant is sent only when the server says this member is locked by the
 * key: put the original key back and the old authenticator works again, and
 * the grant is worth nothing. A grant the server refuses -- a day old, or from
 * a session that has since ended -- is dropped, and the screen asks for a
 * recovery code instead.
 */
function AuthenticatorSection({ user }: { user: User }) {
  const queries = useQueryClient();
  const [password, setPassword] = useState("");
  const [offer, setOffer] = useState<Offer | null>(null);
  const [code, setCode] = useState("");
  const [current, setCurrent] = useState("");
  const [grant, setGrant] = useState<string | null>(() => heldGrant(user.id));

  const status = useQuery({
    queryKey: ["authenticator"],
    queryFn: () => api.get<AuthenticatorStatus>("/me/authenticator"),
  });
  const locked = status.data?.locked_by_key === true;
  const spendGrant = locked && grant !== null;

  const start = useMutation({
    mutationFn: () => api.post<Offer>("/me/authenticator", { current_password: password }),
    onSuccess: (data) => {
      setOffer(data);
      setPassword("");
    },
  });

  const confirm = useMutation({
    mutationFn: () =>
      api.post<{ devices_revoked: number; other_sessions_ended: number }>(
        "/me/authenticator/confirm",
        spendGrant
          ? { token: offer!.token, code, grant }
          : { token: offer!.token, code, current_code: current },
      ),
    onSuccess: () => {
      dropGrant();
      setGrant(null);
      setOffer(null);
      setCode("");
      setCurrent("");
      void queries.invalidateQueries({ queryKey: ["authenticator"] });
      void queries.invalidateQueries({ queryKey: ["recovery-mode"] });
    },
    onError: (problem) => {
      // A refused grant is a refused proof (401). A wrong new code is a 422
      // and says nothing about the grant, which stays for the next try.
      if (spendGrant && (problem as { status?: number }).status === 401) {
        dropGrant();
        setGrant(null);
      }
    },
  });

  if (confirm.isSuccess) {
    return (
      <section>
        <h3 className="section-title">Authenticator</h3>
        <div className="banner info">
          Your new authenticator is the only one that works now.{" "}
          {confirm.data.devices_revoked > 0
            ? `${confirm.data.devices_revoked} trusted ${
                confirm.data.devices_revoked === 1 ? "browser" : "browsers"
              } will ask for a code again.`
            : "No browsers were being trusted."}
          {confirm.data.other_sessions_ended > 0
            ? ` ${confirm.data.other_sessions_ended} other ${
                confirm.data.other_sessions_ended === 1 ? "browser was" : "browsers were"
              } signed out.`
            : ""}
        </div>
      </section>
    );
  }

  const ready = code.trim().length >= 6 && (spendGrant || current.trim().length >= 6);

  if (offer) {
    return (
      <section>
        <h3 className="section-title">Authenticator</h3>
        <Problem error={confirm.error} />
        <p className="small">
          Add this to your authenticator app, then type the six digits it shows.{" "}
          {locked
            ? "Once it is confirmed, keep only this entry for the account in your app: the old one cannot work here, and nor can one you scanned earlier and never confirmed."
            : "Nothing has changed yet — your current authenticator keeps working until a code proves the new one pairs."}
        </p>
        <p className="mono small secret-box">{offer.secret}</p>
        <p className="small muted">
          Or open{" "}
          <a href={offer.uri} className="mono">
            the enrolment link
          </a>{" "}
          on the device with the app.
        </p>
        <Field label="The six digits from the new one">
          <input
            value={code}
            autoFocus
            inputMode="numeric"
            autoComplete="one-time-code"
            onChange={(e) => setCode(e.target.value)}
          />
        </Field>
        <p />
        {spendGrant ? (
          <p className="small muted">
            The recovery code you signed in with covers this, so no other code is asked for.
          </p>
        ) : (
          <Field
            label={
              locked ? "One of your recovery codes" : "A code from your current authenticator"
            }
            hint={
              <Hint label="why this is asked">
                <p>
                  The password alone cannot replace your second factor — otherwise it would not be
                  a second factor.{" "}
                  {locked
                    ? "This server cannot check a code from the authenticator you have, so one of your recovery codes proves it is you instead; it is spent."
                    : "If you no longer have the old authenticator, use one of your recovery codes here instead; it is spent."}
                </p>
              </Hint>
            }
          >
            <input
              value={current}
              autoComplete="off"
              onChange={(e) => setCurrent(e.target.value)}
            />
          </Field>
        )}
        <div className="row" style={{ marginTop: 12 }}>
          <button
            className="primary"
            disabled={!ready || confirm.isPending}
            onClick={() => confirm.mutate()}
          >
            Confirm and replace
          </button>
          <button onClick={() => setOffer(null)}>Cancel</button>
        </div>
        <p className="small muted" style={{ marginTop: 10 }}>
          Replacing it also un-trusts every browser, including this one, and signs out every other
          browser — they were let in on the strength of the old authenticator, and that is exactly
          what you are replacing.
        </p>
      </section>
    );
  }

  return (
    <section>
      <h3 className="section-title">Authenticator</h3>
      <Problem error={start.error} />
      {locked ? (
        <div className="banner warn" role="status">
          This server's secret key was replaced, so the authenticator you have no longer works
          here, and nothing that asks for a code from it can be done until you set up a new one.{" "}
          {spendGrant
            ? "The recovery code you signed in with in this tab covers it — no other code is needed."
            : "Have one of your recovery codes ready: it proves it is you in place of the old authenticator. (The recovery code you signed in with covers it only in the tab where you used it, while you stay signed in, and for a day.)"}
        </div>
      ) : (
        <p className="small muted">
          Set up a new authenticator — a new phone, or one you no longer have. The current one
          keeps working until the new one is proved.
        </p>
      )}
      <Field label="Current password">
        <input
          type="password"
          value={password}
          autoComplete="current-password"
          onChange={(e) => setPassword(e.target.value)}
        />
      </Field>
      <button
        style={{ marginTop: 12 }}
        disabled={password.length === 0 || start.isPending}
        onClick={() => start.mutate()}
      >
        Set up a new authenticator
      </button>
    </section>
  );
}

/**
 * How many recovery codes are left, and making a new set (#289).
 *
 * Asks for the password and a code from the authenticator -- and only the
 * authenticator. A recovery code is not accepted here: the reason to replace
 * the sheet is that somebody else may have seen it, and a code from that sheet
 * must not be able to mint the next one.
 *
 * The new codes are shown once, with the same "I have stored these" box as
 * setup, and the form is gone before they appear so the code that proved it
 * does not sit next to them.
 */
export function RecoveryCodesSection() {
  const queries = useQueryClient();
  const left = useQuery({
    queryKey: ["recovery-codes"],
    queryFn: () => api.get<{ unused: number }>("/me/recovery-codes"),
  });

  const [making, setMaking] = useState(false);
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [fresh, setFresh] = useState<string[] | null>(null);

  const make = useMutation({
    mutationFn: () => api.post<{ codes: string[] }>("/me/recovery-codes", { password, code }),
    onSuccess: (made) => {
      setFresh(made.codes);
      setMaking(false);
      setPassword("");
      setCode("");
      void queries.invalidateQueries({ queryKey: ["recovery-codes"] });
    },
  });

  if (fresh) {
    return (
      <section>
        <h3 className="section-title">Recovery codes</h3>
        <div className="banner info">
          New codes made. Every earlier code, used or not, has stopped working.
        </div>
        <RecoveryCodeSheet codes={fresh} action="Done" onStored={() => setFresh(null)} />
      </section>
    );
  }

  const unused = left.data?.unused;
  const ready = password.length > 0 && /^\d{6}$/.test(code.replace(/\s/g, ""));

  return (
    <section>
      <h3 className="section-title">Recovery codes</h3>
      <Problem error={left.error} />
      {unused !== undefined ? (
        <p className={unused === 0 ? "small neg" : "small muted"}>
          {unused === 0
            ? "You have no unused recovery codes. If you lose your authenticator, you have no way back in on your own."
            : `${unused} unused recovery ${unused === 1 ? "code" : "codes"} left.`}
        </p>
      ) : null}

      {making ? (
        <div className="card" style={{ marginTop: 12 }}>
          <Problem error={make.error} />
          <p className="small muted" style={{ marginTop: 0 }}>
            Every code you have now, used or not, stops working and ten new ones replace them.
            Nobody is signed out.
          </p>
          <Field label="Your password">
            <input
              type="password"
              value={password}
              autoComplete="current-password"
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          <p />
          <Field
            label="The six digits from your authenticator"
            hint={
              <Hint label="why not a recovery code">
                <p>
                  A recovery code cannot be used here. If somebody else has seen your codes, one of
                  them must not be enough to make the next set.
                </p>
              </Hint>
            }
          >
            <input
              value={code}
              inputMode="numeric"
              autoComplete="one-time-code"
              onChange={(e) => setCode(e.target.value)}
            />
          </Field>
          <div className="row" style={{ marginTop: 12 }}>
            <button
              className="primary"
              disabled={!ready || make.isPending}
              onClick={() => make.mutate()}
            >
              {make.isPending ? "Making…" : "Make new codes"}
            </button>
            <button
              onClick={() => {
                setMaking(false);
                setPassword("");
                setCode("");
                make.reset();
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button style={{ marginTop: 12 }} onClick={() => setMaking(true)}>
          Make new codes
        </button>
      )}
    </section>
  );
}

/**
 * The keys you have given to programs.
 *
 * Two things this screen refuses to do, and both are deliberate.
 *
 * It does not offer to show a token again. Only the SHA-256 is stored, so
 * there is nothing to show — which is the same property that makes revoking a
 * key take effect on its very next request.
 *
 * It does not offer to widen a key's scope. A key's reach is fixed when it is
 * issued; changing your mind means issuing a new one, which costs a minute and
 * removes a whole class of "who widened this, and when".
 */
function KeysSection({ households }: { households: Household[] }) {
  const queries = useQueryClient();
  const keys = useQuery({
    queryKey: ["agent-keys"],
    queryFn: () => api.get<AgentKey[]>("/me/keys"),
  });

  const [issuing, setIssuing] = useState(false);
  const [fresh, setFresh] = useState<{ token: string; label: string } | null>(null);

  const revoke = useMutation({
    mutationFn: (id: string) => api.post<AgentKey>(`/me/keys/${id}/revoke`),
    onSuccess: () => queries.invalidateQueries({ queryKey: ["agent-keys"] }),
  });

  const named = (id: string) => households.find((h) => h.id === id)?.name ?? "a household";

  return (
    <section>
      <h3 className="section-title">Keys for programs</h3>
      <p className="small muted" style={{ marginTop: 0 }}>
        A key lets a program — a script, an automation, an AI assistant — read and, if you
        allow it, add to one household on your behalf. Everything it does appears in that
        household's History under your name and the key's.
      </p>

      {fresh && (
        <div className="banner info">
          <p style={{ marginTop: 0 }}>
            <strong>Copy this now.</strong> It is the only time it is shown — only a
            fingerprint of it is stored, so there is nothing to show again.
          </p>
          <p className="mono small secret-box" style={{ wordBreak: "break-all" }}>
            {fresh.token}
          </p>
          <button onClick={() => setFresh(null)}>I have saved it</button>
        </div>
      )}

      <Problem error={keys.error ?? revoke.error} />

      {keys.data && keys.data.length === 0 && !issuing ? (
        <Empty>You have not given a key to anything.</Empty>
      ) : null}

      {keys.data && keys.data.length > 0 ? (
        <ul className="plain-list">
          {keys.data.map((key) => (
            <li key={key.id} className="key-row">
              <div>
                <strong>{key.label}</strong>
                {key.agent_name ? <span className="muted"> · {key.agent_name}</span> : null}
                <div className="small muted">
                  {named(key.household_id)} · {key.scopes.join(" and ")}
                  {key.may_commit ? " · may apply imports" : ""}
                </div>
                <div className="small muted">
                  {key.revoked_at
                    ? `Revoked ${formatInstant(key.revoked_at)}`
                    : key.live
                      ? `Expires ${formatInstant(key.expires_at)}`
                      : `Expired ${formatInstant(key.expires_at)}`}
                  {key.last_used_at
                    ? ` · last used ${formatInstant(key.last_used_at)}`
                    : " · never used"}
                </div>
              </div>
              {key.live ? (
                <button
                  className="danger"
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(key.id)}
                >
                  Revoke
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      {issuing ? (
        <IssueKey
          households={households}
          onDone={(token, label) => {
            setIssuing(false);
            setFresh({ token, label });
            void queries.invalidateQueries({ queryKey: ["agent-keys"] });
          }}
          onCancel={() => setIssuing(false)}
        />
      ) : (
        <button style={{ marginTop: 12 }} onClick={() => setIssuing(true)}>
          Give a program a key
        </button>
      )}
    </section>
  );
}

/**
 * Issuing, in one step that asks for both factors.
 *
 * The step-up grant is fetched and spent inside a single click, so the token
 * never sits in the page waiting to be used. It is single-use and dies in five
 * minutes either way.
 */
function IssueKey({
  households,
  onDone,
  onCancel,
}: {
  households: Household[];
  onDone: (token: string, label: string) => void;
  onCancel: () => void;
}) {
  const [label, setLabel] = useState("");
  const [agentName, setAgentName] = useState("");
  const [householdId, setHouseholdId] = useState(households[0]?.id ?? "");
  const [mayWrite, setMayWrite] = useState(false);
  const [mayCommit, setMayCommit] = useState(false);
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");

  const issue = useMutation({
    mutationFn: async () => {
      // Both calls in one action. A grant handed to the page and left there
      // would be a five-minute window in which the screen, not the person,
      // holds the authority.
      const token = await stepUpToken({ password, code });
      return api.post<{ key: AgentKey; token: string }>("/me/keys", {
        step_up_token: token,
        label,
        agent_name: agentName.trim() || null,
        household_id: householdId,
        scope: mayWrite ? "write" : "read",
        may_commit: mayWrite && mayCommit,
      });
    },
    onSuccess: (made) => onDone(made.token, made.key.label),
  });

  const ready =
    label.trim().length > 0 && householdId.length > 0 && password.length > 0 && code.trim().length >= 6;

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <Problem error={issue.error} />

      <Field label="What is it for">
        <input
          value={label}
          autoFocus
          placeholder="receipt filer"
          onChange={(e) => setLabel(e.target.value)}
        />
      </Field>
      <p />
      <Field label="What is holding it (optional)">
        <input
          value={agentName}
          placeholder="Claude Desktop"
          onChange={(e) => setAgentName(e.target.value)}
        />
      </Field>
      <p />
      <Field label="Which household">
        <select value={householdId} onChange={(e) => setHouseholdId(e.target.value)}>
          {households.map((house) => (
            <option key={house.id} value={house.id}>
              {house.name}
            </option>
          ))}
        </select>
      </Field>
      <p className="small muted">One household per key. Two households means two keys.</p>

      {/*
        Not a <Field>: its hint variant wraps children in a <label>, and a
        checkbox label nested inside that is invalid HTML — which is exactly
        how this first rendered, with the words squeezed into a column three
        of them wide. The heading and the hint are laid out directly instead.
      */}
      <div className="field">
        <div className="field-head">
          <span>What it may do</span>
          <Hint label="what a key can never do">
            <p>
              No key can delete anything, undo anything, change who is in a household, touch
              passwords or authenticators, or create another key — whatever you choose here.
            </p>
            <p className="muted small" style={{ marginBottom: 0 }}>
              Everything a program gets wrong has to be undoable by a person, which means a
              person stays the only one who can undo.
            </p>
          </Hint>
        </div>
        <label className="check" style={{ marginTop: 6 }}>
          <input
            type="checkbox"
            checked={mayWrite}
            onChange={(e) => {
              setMayWrite(e.target.checked);
              if (!e.target.checked) setMayCommit(false);
            }}
          />
          Let it add and change transactions, not only read them
        </label>
      </div>
      {mayWrite ? (
        <label className="check" style={{ marginTop: 6 }}>
          <input
            type="checkbox"
            checked={mayCommit}
            onChange={(e) => setMayCommit(e.target.checked)}
          />
          Let it apply an import without anybody reviewing it
        </label>
      ) : null}
      {mayWrite && mayCommit ? (
        <p className="small muted">
          Normally a program stages an import and you review it before anything lands in the
          register. This skips that.
        </p>
      ) : null}

      <hr className="rule" />
      <p className="small muted" style={{ marginTop: 0 }}>
        A key keeps working from somewhere else long after this browser is closed, so this asks
        for your password and a code — the same as signing in.
      </p>
      <Field label="Your password">
        <input
          type="password"
          value={password}
          autoComplete="current-password"
          onChange={(e) => setPassword(e.target.value)}
        />
      </Field>
      <p />
      <Field label="The six digits from your authenticator">
        <input
          value={code}
          inputMode="numeric"
          autoComplete="one-time-code"
          onChange={(e) => setCode(e.target.value)}
        />
      </Field>

      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={!ready || issue.isPending} onClick={() => issue.mutate()}>
          {issue.isPending ? "Creating…" : "Create the key"}
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
