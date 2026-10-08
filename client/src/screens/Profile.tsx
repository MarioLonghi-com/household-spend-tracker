/**
 * Your own account: how you sign in, and the keys you have given to programs.
 *
 * **Sign-in methods** (#122, decision 5 in #47) is one section, not four: the
 * password, the authenticator, passkeys and recovery codes are rows, each
 * saying its state in words and offering its one or two actions, under a line
 * that says what currently gets you in. Keys for programs stay apart -- they
 * are for programs, not for signing in.
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
import { PasskeysSection, usePasskeys, usePasskeyState } from "./Passkeys";
import { formatInstant } from "../lib/time";
import { dropGrant, heldGrant } from "../lib/recoveryGrant";
import {
  type Appearance,
  chooseAppearance,
  effectiveScheme,
  storedAppearance,
} from "../lib/appearance";
import type { AgentKey, AuthenticatorStatus, Household, User } from "../lib/types";
import { LanguagePicker } from "../components/LanguagePicker";
import { plural, t } from "@lingui/core/macro";
import { Trans, useLingui } from "@lingui/react/macro";
import { listText } from "../lib/locale";

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
  const { t } = useLingui();
  return (
    <Panel title={t`Your account`} onClose={onClose} config>
      <p className="muted small" style={{ marginTop: 0 }}>
        <Trans>
          Signed in as <strong>{user.display_name}</strong> ({user.email}).
        </Trans>
      </p>
      <AppearanceSection />
      <hr className="rule" />
      <SignInMethods user={user} />
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
  const { t } = useLingui();
  const [choice, setChoice] = useState<Appearance>(storedAppearance);

  const pick = (next: Appearance) => {
    setChoice(next);
    chooseAppearance(next);
  };

  const options: { key: Appearance; label: string; icon: string }[] = [
    { key: "light", label: t`Light`, icon: "\u2600\ufe0f" },
    { key: "dark", label: t`Dark`, icon: "\ud83c\udf19" },
    { key: "system", label: t`System`, icon: "\ud83d\udcbb" },
  ];

  return (
    <section>
      <h3>
        <Trans>Appearance</Trans>{" "}
        <Hint label={t`about appearance`}>
          <Trans>
            Kept on this device only, so a phone and a laptop can differ. Nothing is sent to the
            server, and clearing this browser's site data forgets it -- which returns you to
            following the system.
          </Trans>
        </Hint>
      </h3>

      <div className="appearance-choices" role="group" aria-label={t`Appearance`}>
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
          ? effectiveScheme("system") === "dark"
            ? t`Following this device, which is dark right now.`
            : t`Following this device, which is light right now.`
          : choice === "dark"
            ? t`Held in dark, whatever this device is set to.`
            : t`Held in light, whatever this device is set to.`}
      </p>

      <LanguagePicker />
    </section>
  );
}

type Method = "password" | "authenticator" | "recovery";

/**
 * Every way into this account, in one place (#47 §3).
 *
 * The password, authenticator and recovery-code rows open their forms in
 * place, one at a time; the passkeys row is its own list. The summary line at
 * the top changes as methods are added or removed, so it always says what
 * currently gets you in.
 */
function SignInMethods({ user }: { user: User }) {
  const { t } = useLingui();
  const [open, setOpen] = useState<Method | null>(null);
  const authenticator = useQuery({
    queryKey: ["authenticator"],
    queryFn: () => api.get<AuthenticatorStatus>("/me/authenticator"),
  });
  const recovery = useQuery({
    queryKey: ["recovery-codes"],
    queryFn: () => api.get<{ unused: number }>("/me/recovery-codes"),
  });
  const passkeys = usePasskeys();
  const state = usePasskeyState();

  const usable = (passkeys.data ?? []).filter((one) => one.usable_here).length;
  const locked = authenticator.data?.locked_by_key === true;
  const enrolled = authenticator.data?.enrolled !== false;
  const summary =
    usable > 0
      ? t`You sign in with a passkey, or with your password and authenticator code.`
      : t`You sign in with your password and authenticator code.`;

  const toggle = (method: Method) => setOpen((was) => (was === method ? null : method));
  const passkeyState = passkeys.data
    ? usable > 0
      ? plural(usable, { one: `${usable} passkey`, other: `${usable} passkeys` })
      : state.data && !state.data.available
        ? t`Not available here`
        : t`None yet`
    : "";

  return (
    <section aria-labelledby="sign-in-methods">
      <h3 className="section-title" id="sign-in-methods">
        <Trans>Sign-in methods</Trans>
      </h3>
      <p className="small" style={{ marginTop: 0 }}>
        {summary}
      </p>
      <ul className="plain-list methods">
        <MethodRow
          slug="password"
          name={t`Password`}
          state={t`Set`}
          action={t`Change`}
          open={open === "password"}
          onToggle={() => toggle("password")}
        >
          <PasswordSection titled={false} />
        </MethodRow>
        <MethodRow
          slug="authenticator"
          name={t`Authenticator`}
          state={!enrolled ? t({ message: "Cleared", context: "authenticator state" }) : locked ? t`Needs setting up again` : t`Set up`}
          warn={locked || !enrolled}
          // In recovery mode (#287) this row is the one thing to do, so it
          // stays open and offers no way to close it.
          action={locked ? undefined : t`Set up again`}
          open={open === "authenticator" || locked}
          onToggle={() => toggle("authenticator")}
        >
          <AuthenticatorSection user={user} titled={false} />
        </MethodRow>
        <MethodRow slug="passkeys" name={t`Passkeys`} state={passkeyState} open>
          <PasskeysSection />
        </MethodRow>
        <MethodRow
          slug="recovery-codes"
          name={t`Recovery codes`}
          state={recovery.data ? t`${recovery.data.unused} of 10 left` : ""}
          warn={recovery.data?.unused === 0}
          action={t`New codes`}
          open={open === "recovery"}
          onToggle={() => toggle("recovery")}
        >
          <RecoveryCodesSection titled={false} />
        </MethodRow>
      </ul>
    </section>
  );
}

/** One sign-in method: its name, its state in words, its action. */
function MethodRow({
  slug,
  name,
  state,
  warn = false,
  action,
  open,
  onToggle,
  children,
}: {
  /** The row's id, the English name as it always was, whatever the language. */
  slug: string;
  name: string;
  state: string;
  warn?: boolean;
  action?: string;
  open: boolean;
  onToggle?: () => void;
  children: React.ReactNode;
}) {
  const { t } = useLingui();
  const id = `method-${slug}`;
  return (
    <li className="method-row" aria-labelledby={id}>
      <div className="method-head">
        <div>
          <strong id={id}>{name}</strong>{" "}
          <span className={warn ? "small neg" : "small muted"}>{state}</span>
        </div>
        {action && onToggle && (
          <button
            type="button"
            aria-expanded={open}
            // The visible word alone ("Change") is ambiguous in a list of four.
            aria-label={open ? t`Close: ${name}` : t`${action}: ${name}`}
            onClick={onToggle}
          >
            {open ? t`Close` : action}
          </button>
        )}
      </div>
      {open && <div className="method-body">{children}</div>}
    </li>
  );
}

/**
 * What a password change did beyond the password: other browsers signed out,
 * trusted ones forgotten, agent keys left working. One sentence each.
 */
function passwordChangedText(done: {
  other_sessions_ended: number;
  devices_revoked: number;
  keys_still_live: number;
}): string {
  const ended = done.other_sessions_ended;
  const revoked = done.devices_revoked;
  const keys = done.keys_still_live;
  let text =
    ended > 0
      ? plural(ended, {
          one: `${ended} other browser was signed out.`,
          other: `${ended} other browsers were signed out.`,
        })
      : t`No other browsers were signed in.`;
  if (revoked > 0)
    text += ` ${plural(revoked, {
      one: `${revoked} trusted browser will ask for a code again.`,
      other: `${revoked} trusted browsers will ask for a code again.`,
    })}`;
  if (keys > 0)
    text += ` ${plural(keys, {
      one: `${keys} agent key still works — revoke it below if it should stop.`,
      other: `${keys} agent keys still work — revoke them below if they should stop.`,
    })}`;
  return text;
}

function PasswordSection({ titled = true }: { titled?: boolean }) {
  const { t } = useLingui();
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
      {titled && (
        <h3 className="section-title">
          <Trans>Password</Trans>
        </h3>
      )}
      <Problem error={change.error} />
      {change.isSuccess && (
        <div className="banner info">
          <Trans>Password changed.</Trans>{" "}
          {passwordChangedText(change.data)}
        </div>
      )}

      <Field label={t`Current password`}>
        <input
          type="password"
          value={current}
          autoComplete="current-password"
          onChange={(e) => setCurrent(e.target.value)}
        />
      </Field>
      <p />
      <Field
        label={t`New password`}
        hint={
          <Hint label={t`what makes a good one`}>
            <p>
              <Trans>
                Length does more than symbols do. Four unrelated words you can actually remember
                beats a short one with punctuation in it.
              </Trans>
            </p>
            <p className="muted small" style={{ marginBottom: 0 }}>
              <Trans>
                Changing it signs out every other browser and forgets every trusted one, so if
                you are changing it because somebody else knows it, they are out too. Agent keys
                are separate credentials and keep working until you revoke them.
              </Trans>
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
      <Field label={t`New password again`}>
        <input
          type="password"
          value={again}
          autoComplete="new-password"
          onChange={(e) => setAgain(e.target.value)}
        />
      </Field>
      {mismatch ? (
        <p className="small neg">
          <Trans>Those two don't match.</Trans>
        </p>
      ) : null}

      <button
        className="primary"
        style={{ marginTop: 12 }}
        disabled={!ready || change.isPending}
        onClick={() => change.mutate()}
      >
        <Trans>Change password</Trans>
      </button>
    </section>
  );
}

/** What replacing the authenticator did to other browsers, one sentence each. */
function replacedText(done: { devices_revoked: number; other_sessions_ended: number }): string {
  const revoked = done.devices_revoked;
  const ended = done.other_sessions_ended;
  let text =
    revoked > 0
      ? plural(revoked, {
          one: `${revoked} trusted browser will ask for a code again.`,
          other: `${revoked} trusted browsers will ask for a code again.`,
        })
      : t`No browsers were being trusted.`;
  if (ended > 0)
    text += ` ${plural(ended, {
      one: `${ended} other browser was signed out.`,
      other: `${ended} other browsers were signed out.`,
    })}`;
  return text;
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
function AuthenticatorSection({ user, titled = true }: { user: User; titled?: boolean }) {
  const { t } = useLingui();
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
        {titled && (
          <h3 className="section-title">
            <Trans>Authenticator</Trans>
          </h3>
        )}
        <div className="banner info">
          <Trans>Your new authenticator is the only one that works now.</Trans>{" "}
          {replacedText(confirm.data)}
        </div>
      </section>
    );
  }

  const ready = code.trim().length >= 6 && (spendGrant || current.trim().length >= 6);

  if (offer) {
    return (
      <section>
        {titled && (
          <h3 className="section-title">
            <Trans>Authenticator</Trans>
          </h3>
        )}
        <Problem error={confirm.error} />
        <p className="small">
          <Trans>Add this to your authenticator app, then type the six digits it shows.</Trans>{" "}
          {locked
            ? t`Once it is confirmed, keep only this entry for the account in your app: the old one cannot work here, and nor can one you scanned earlier and never confirmed.`
            : t`Nothing has changed yet — your current authenticator keeps working until a code proves the new one pairs.`}
        </p>
        <p className="mono small secret-box">{offer.secret}</p>
        <p className="small muted">
          <Trans>
            Or open{" "}
            <a href={offer.uri} className="mono">
              the enrolment link
            </a>{" "}
            on the device with the app.
          </Trans>
        </p>
        <Field label={t`The six digits from the new one`}>
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
            <Trans>
              The recovery code you signed in with covers this, so no other code is asked for.
            </Trans>
          </p>
        ) : (
          <Field
            label={
              locked ? t`One of your recovery codes` : t`A code from your current authenticator`
            }
            hint={
              <Hint label={t`why this is asked`}>
                <p>
                  <Trans>
                    The password alone cannot replace your second factor — otherwise it would not
                    be a second factor.
                  </Trans>{" "}
                  {locked
                    ? t`This server cannot check a code from the authenticator you have, so one of your recovery codes proves it is you instead; it is spent.`
                    : t`If you no longer have the old authenticator, use one of your recovery codes here instead; it is spent.`}
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
            <Trans>Confirm and replace</Trans>
          </button>
          <button onClick={() => setOffer(null)}>
            <Trans>Cancel</Trans>
          </button>
        </div>
        <p className="small muted" style={{ marginTop: 10 }}>
          <Trans>
            Replacing it also un-trusts every browser, including this one, and signs out every
            other browser — they were let in on the strength of the old authenticator, and that is
            exactly what you are replacing.
          </Trans>
        </p>
      </section>
    );
  }

  return (
    <section>
      {titled && (
        <h3 className="section-title">
          <Trans>Authenticator</Trans>
        </h3>
      )}
      <Problem error={start.error} />
      {locked ? (
        <div className="banner warn" role="status">
          <Trans>
            This server's secret key was replaced, so the authenticator you have no longer works
            here, and nothing that asks for a code from it can be done until you set up a new one.
          </Trans>{" "}
          {spendGrant
            ? t`The recovery code you signed in with in this tab covers it — no other code is needed.`
            : t`Have one of your recovery codes ready: it proves it is you in place of the old authenticator. (The recovery code you signed in with covers it only in the tab where you used it, while you stay signed in, and for a day.)`}
        </div>
      ) : (
        <p className="small muted">
          <Trans>
            Set up a new authenticator — a new phone, or one you no longer have. The current one
            keeps working until the new one is proved.
          </Trans>
        </p>
      )}
      <Field label={t`Current password`}>
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
        <Trans>Set up a new authenticator</Trans>
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
export function RecoveryCodesSection({ titled = true }: { titled?: boolean }) {
  const { t } = useLingui();
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
        {titled && (
          <h3 className="section-title">
            <Trans>Recovery codes</Trans>
          </h3>
        )}
        <div className="banner info">
          <Trans>New codes made. Every earlier code, used or not, has stopped working.</Trans>
        </div>
        <RecoveryCodeSheet codes={fresh} action={t`Done`} onStored={() => setFresh(null)} />
      </section>
    );
  }

  const unused = left.data?.unused;
  const ready = password.length > 0 && /^\d{6}$/.test(code.replace(/\s/g, ""));

  return (
    <section>
      {titled && (
        <h3 className="section-title">
          <Trans>Recovery codes</Trans>
        </h3>
      )}
      <Problem error={left.error} />
      {unused !== undefined ? (
        <p className={unused === 0 ? "small neg" : "small muted"}>
          {unused === 0
            ? t`You have no unused recovery codes. If you lose your authenticator, you have no way back in on your own.`
            : plural(unused, {
                one: `${unused} unused recovery code left.`,
                other: `${unused} unused recovery codes left.`,
              })}
        </p>
      ) : null}

      {making ? (
        <div className="card" style={{ marginTop: 12 }}>
          <Problem error={make.error} />
          <p className="small muted" style={{ marginTop: 0 }}>
            <Trans>
              Every code you have now, used or not, stops working and ten new ones replace them.
              Nobody is signed out.
            </Trans>
          </p>
          <Field label={t`Your password`}>
            <input
              type="password"
              value={password}
              autoComplete="current-password"
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          <p />
          <Field
            label={t`The six digits from your authenticator`}
            hint={
              <Hint label={t`why not a recovery code`}>
                <p>
                  <Trans>
                    A recovery code cannot be used here. If somebody else has seen your codes, one
                    of them must not be enough to make the next set.
                  </Trans>
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
              {make.isPending ? t`Making…` : t`Make new codes`}
            </button>
            <button
              onClick={() => {
                setMaking(false);
                setPassword("");
                setCode("");
                make.reset();
              }}
            >
              <Trans>Cancel</Trans>
            </button>
          </div>
        </div>
      ) : (
        <button style={{ marginTop: 12 }} onClick={() => setMaking(true)}>
          <Trans>Make new codes</Trans>
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
/** A key's scope, as the list says it. */
function scopeWord(scope: string): string {
  if (scope === "read") return t`read`;
  if (scope === "write") return t`write`;
  return scope;
}

function KeysSection({ households }: { households: Household[] }) {
  const { t } = useLingui();
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

  const named = (id: string) => households.find((h) => h.id === id)?.name ?? t`a household`;

  return (
    <section>
      <h3 className="section-title">
        <Trans>Keys for programs</Trans>
      </h3>
      <p className="small muted" style={{ marginTop: 0 }}>
        <Trans>
          A key lets a program — a script, an automation, an AI assistant — read and, if you
          allow it, add to one household on your behalf. Everything it does appears in that
          household's History under your name and the key's.
        </Trans>
      </p>

      {fresh && (
        <div className="banner info">
          <p style={{ marginTop: 0 }}>
            <Trans>
              <strong>Copy this now.</strong> It is the only time it is shown — only a
              fingerprint of it is stored, so there is nothing to show again.
            </Trans>
          </p>
          <p className="mono small secret-box" style={{ wordBreak: "break-all" }}>
            {fresh.token}
          </p>
          <button onClick={() => setFresh(null)}>
            <Trans>I have saved it</Trans>
          </button>
        </div>
      )}

      <Problem error={keys.error ?? revoke.error} />

      {keys.data && keys.data.length === 0 && !issuing ? (
        <Empty>
          <Trans>You have not given a key to anything.</Trans>
        </Empty>
      ) : null}

      {keys.data && keys.data.length > 0 ? (
        <ul className="plain-list">
          {keys.data.map((key) => (
            <li key={key.id} className="key-row">
              <div>
                <strong>{key.label}</strong>
                {key.agent_name ? <span className="muted"> · {key.agent_name}</span> : null}
                <div className="small muted">
                  {named(key.household_id)} · {listText(key.scopes.map(scopeWord))}
                  {key.may_commit ? ` · ${t`may apply imports`}` : ""}
                </div>
                <div className="small muted">
                  {key.revoked_at
                    ? t`Revoked ${formatInstant(key.revoked_at)}`
                    : key.live
                      ? t`Expires ${formatInstant(key.expires_at)}`
                      : t`Expired ${formatInstant(key.expires_at)}`}
                  {key.last_used_at
                    ? ` · ${t`last used ${formatInstant(key.last_used_at)}`}`
                    : ` · ${t`never used`}`}
                </div>
              </div>
              {key.live ? (
                <button
                  className="danger"
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(key.id)}
                >
                  <Trans>Revoke</Trans>
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
          <Trans>Give a program a key</Trans>
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
  const { t } = useLingui();
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

      <Field label={t`What is it for`}>
        <input
          value={label}
          autoFocus
          placeholder={t`receipt filer`}
          onChange={(e) => setLabel(e.target.value)}
        />
      </Field>
      <p />
      <Field label={t`What is holding it (optional)`}>
        <input
          value={agentName}
          placeholder="Claude Desktop"
          onChange={(e) => setAgentName(e.target.value)}
        />
      </Field>
      <p />
      <Field label={t`Which household`}>
        <select value={householdId} onChange={(e) => setHouseholdId(e.target.value)}>
          {households.map((house) => (
            <option key={house.id} value={house.id}>
              {house.name}
            </option>
          ))}
        </select>
      </Field>
      <p className="small muted">
        <Trans>One household per key. Two households means two keys.</Trans>
      </p>

      {/*
        Not a <Field>: its hint variant wraps children in a <label>, and a
        checkbox label nested inside that is invalid HTML — which is exactly
        how this first rendered, with the words squeezed into a column three
        of them wide. The heading and the hint are laid out directly instead.
      */}
      <div className="field">
        <div className="field-head">
          <span>
            <Trans>What it may do</Trans>
          </span>
          <Hint label={t`what a key can never do`}>
            <p>
              <Trans>
                No key can delete anything, undo anything, change who is in a household, touch
                passwords or authenticators, or create another key — whatever you choose here.
              </Trans>
            </p>
            <p className="muted small" style={{ marginBottom: 0 }}>
              <Trans>
                Everything a program gets wrong has to be undoable by a person, which means a
                person stays the only one who can undo.
              </Trans>
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
          <Trans>Let it add and change transactions, not only read them</Trans>
        </label>
      </div>
      {mayWrite ? (
        <label className="check" style={{ marginTop: 6 }}>
          <input
            type="checkbox"
            checked={mayCommit}
            onChange={(e) => setMayCommit(e.target.checked)}
          />
          <Trans>Let it apply an import without anybody reviewing it</Trans>
        </label>
      ) : null}
      {mayWrite && mayCommit ? (
        <p className="small muted">
          <Trans>
            Normally a program stages an import and you review it before anything lands in the
            register. This skips that.
          </Trans>
        </p>
      ) : null}

      <hr className="rule" />
      <p className="small muted" style={{ marginTop: 0 }}>
        <Trans>
          A key keeps working from somewhere else long after this browser is closed, so this asks
          for your password and a code — the same as signing in.
        </Trans>
      </p>
      <Field label={t`Your password`}>
        <input
          type="password"
          value={password}
          autoComplete="current-password"
          onChange={(e) => setPassword(e.target.value)}
        />
      </Field>
      <p />
      <Field label={t`The six digits from your authenticator`}>
        <input
          value={code}
          inputMode="numeric"
          autoComplete="one-time-code"
          onChange={(e) => setCode(e.target.value)}
        />
      </Field>

      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={!ready || issue.isPending} onClick={() => issue.mutate()}>
          {issue.isPending ? t`Creating…` : t`Create the key`}
        </button>
        <button onClick={onCancel}>
          <Trans>Cancel</Trans>
        </button>
      </div>
    </div>
  );
}
