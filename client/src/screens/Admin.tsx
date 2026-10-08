/**
 * The admin panel: people, households, and the invitations that create both.
 *
 * Owner-only, and the server says so too -- every call here is 403 for a
 * member, so hiding the nav button is a courtesy and not the control.
 *
 * Making an owner -- inviting one, or promoting a member -- asks for the
 * password and a code as well (#205): an owner account outlives this session
 * and every reset of your own credentials, so a session cookie alone is not
 * enough, the same bar as minting an agent key.
 *
 * Resetting somebody's sign-in (#286) asks for neither, by decision (#283):
 * any account, another owner's included. What stands in for the step-up is
 * that it is seen -- the person reset is told who did it on the link page,
 * and every owner sees it under People, in Recent sign-in changes, read from
 * the audit log.
 */

import { useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import {
  Dialog,
  Empty,
  Field,
  Panel,
  Problem,
  SortHeading,
  sortRows,
  useSort,
} from "../components/bits";
import {
  NO_PROOF,
  StepUpFields,
  stepUpReady,
  stepUpToken,
  type StepUpProof,
} from "../components/StepUp";
import { formatInstant } from "../lib/time";
import { byText, switchesText, whatText } from "../lib/signInChanges";
import type {
  AdminHousehold,
  AdminUser,
  Invitation,
  InviteCreated,
  PendingReset,
  ResetIssued,
  Role,
  SignInChange,
  User,
} from "../lib/types";
import { roleLabel } from "../lib/labels";
import { compareNames } from "../lib/locale";

type Section = "people" | "households" | "invitations";

type PeopleSort = "name" | "email" | "role" | "households" | "codes" | "status";
type HouseholdSort = "name" | "currency" | "dates" | "members";
type InviteSort = "for" | "role" | "households" | "expires";
type PendingSort = "who" | "resets" | "by" | "date" | "expires";
type ChangeSort = "date" | "what" | "whom" | "by";

const PENDING_KEY = ["admin", "resets"];
const readPending = () => api.get<PendingReset[]>("/admin/resets");

const SECTIONS: { key: Section; label: string }[] = [
  { key: "people", label: "People" },
  { key: "households", label: "Households" },
  { key: "invitations", label: "Invitations" },
];

export function Admin({ user }: { user: User }) {
  const [section, setSection] = useState<Section>("people");

  const users = useQuery({
    queryKey: ["admin", "users"],
    queryFn: () => api.get<AdminUser[]>("/admin/users"),
  });
  const households = useQuery({
    queryKey: ["admin", "households"],
    queryFn: () => api.get<AdminHousehold[]>("/admin/households"),
  });

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
        <h1>Admin</h1>
      </div>
      <p className="muted small">
        Everything on this screen is recorded as a batch with your name on it. These are
        instance-wide rather than household changes, so they are kept out of a household's History.
      </p>

      <div className="row" style={{ gap: 6, marginBottom: 16 }}>
        {SECTIONS.map((one) => (
          <button
            key={one.key}
            aria-current={section === one.key ? "page" : undefined}
            className={section === one.key ? "primary" : undefined}
            onClick={() => setSection(one.key)}
          >
            {one.label}
          </button>
        ))}
      </div>

      <Problem error={users.error ?? households.error} />

      {section === "people" && (
        <>
          <People
            me={user}
            users={users.data ?? []}
            households={households.data ?? []}
            onInvite={() => setSection("invitations")}
          />
          <PendingResets />
          <RecentSignInChanges />
        </>
      )}
      {section === "households" && (
        <Households households={households.data ?? []} users={users.data ?? []} />
      )}
      {section === "invitations" && <Invitations households={households.data ?? []} />}
    </>
  );
}

// --------------------------------------------------------------------------- //
// People
// --------------------------------------------------------------------------- //

function People({
  me,
  users,
  households,
  onInvite,
}: {
  me: User;
  users: AdminUser[];
  households: AdminHousehold[];
  /** The Invitations tab, which is the only way a new account is made. */
  onInvite: () => void;
}) {
  const client = useQueryClient();
  const refresh = () => client.invalidateQueries({ queryKey: ["admin"] });

  //: A role change or a disable, asked about before it is sent (#199). A
  //: dropdown that promotes on change, and a Disable that signs somebody out
  //: everywhere, were both one stray click.
  const [confirming, setConfirming] = useState<
    { user: AdminUser; role: Role } | { user: AdminUser; disable: true } | null
  >(null);
  const setRole = useMutation({
    mutationFn: ({ id, role }: { id: string; role: Role }) =>
      api.post<AdminUser>(`/admin/users/${id}/role`, { role }),
    onSuccess: () => {
      setConfirming(null);
      return refresh();
    },
  });
  // Promotion to owner goes through a dialog that asks for both factors;
  // demotion is the safe direction and stays one change of the dropdown.
  const [promoting, setPromoting] = useState<AdminUser | null>(null);
  const setDisabled = useMutation({
    mutationFn: ({ id, disabled }: { id: string; disabled: boolean }) =>
      api.post<AdminUser>(`/admin/users/${id}/disabled`, { disabled }),
    onSuccess: () => {
      setConfirming(null);
      return refresh();
    },
  });
  //: Whose sign-in is being reset (#286). The dialog holds the link once it
  //: is made, so it stays open across the refresh that follows.
  const [resetting, setResetting] = useState<AdminUser | null>(null);
  // The list below asks for the same; this copy is for the dialog to say a
  // link is already pending for the person it resets.
  const pending = useQuery({ queryKey: PENDING_KEY, queryFn: readPending });

  const householdName = (id: string) => households.find((h) => h.id === id)?.name ?? id;

  const order = useSort<PeopleSort>("name");
  const rows = useMemo(
    () =>
      sortRows(
        users,
        order.sort,
        order.direction,
        (one, column) => {
          switch (column) {
            case "name":
              return one.display_name;
            case "email":
              return one.email;
            case "role":
              return one.role;
            case "households":
              // How many they are in, then which: "who has access to nothing"
              // is the question this column gets asked, and a name sort buries it.
              return [one.households.length, one.households.map(householdName).join(", ")];
            case "codes":
              return one.recovery_codes_left;
            default:
              // Disabled sorts as the exception it is, not as the letter D.
              return one.disabled_at ? 1 : 0;
          }
        },
        (a, b) => compareNames(a.display_name, b.display_name),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [users, households, order.sort, order.direction],
  );

  return (
    <div className="card">
      <Problem error={confirming ? null : (setRole.error ?? setDisabled.error)} />
      {/* Where somebody looking for "add user" will look: at the top of the
          list of users, before they hunt for a button that is not here. There
          is no create-a-user form anywhere, on purpose -- an account is made
          by the person it belongs to, from a link, so they choose their own
          password and nobody else ever knows it. */}
      <p className="muted small" style={{ marginTop: 0 }}>
        To create a new user, use the{" "}
        <button className="link" onClick={onInvite}>
          Invitation link
        </button>
        .
      </p>
      {users.length === 0 ? (
        <Empty>Nobody yet, which should be impossible while you are reading this.</Empty>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <SortHeading
                  label="Name"
                  column="name"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="Email"
                  column="email"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="Role"
                  column="role"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="Households"
                  column="households"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="Recovery codes"
                  column="codes"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="Status"
                  column="status"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((one) => (
                <tr key={one.id}>
                  <td>
                    {one.display_name}
                    {one.id === me.id ? <span className="muted small"> (you)</span> : null}
                  </td>
                  <td className="small muted">{one.email}</td>
                  <td>
                    <select
                      value={one.role}
                      aria-label={`Role for ${one.display_name}`}
                      // Demoting yourself is allowed by the server while another
                      // owner exists -- and the next refetch of this screen then
                      // 403s, leaving a stale table under an error. Make it a
                      // deliberate act rather than a mis-click in a dropdown.
                      disabled={one.id === me.id}
                      title={
                        one.id === me.id
                          ? "Changing your own role would close this screen; ask the other owner"
                          : undefined
                      }
                      onChange={(e) => {
                        const role = e.target.value as Role;
                        // Promotion asks for both factors in its own dialog;
                        // demotion is confirmed here.
                        if (role === "owner" && one.role !== "owner") setPromoting(one);
                        else setConfirming({ user: one, role });
                      }}
                    >
                      <option value="member">Member</option>
                      <option value="owner">Owner</option>
                    </select>
                  </td>
                  <td className="small muted">
                    {one.households.length === 0
                      ? "none"
                      : one.households.map(householdName).join(", ")}
                  </td>
                  <td className="small">
                    <span className={one.recovery_codes_left === 0 ? "neg" : "muted"}>
                      {one.recovery_codes_left} left
                    </span>
                  </td>
                  <td className="small">
                    {one.disabled_at ? (
                      <span className="neg">Disabled</span>
                    ) : (
                      <span className="muted">Active</span>
                    )}
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button
                      className="link"
                      style={{ marginRight: 12 }}
                      aria-label={`Reset sign-in for ${one.display_name}`}
                      // Your own goes through the profile, with proof you hold
                      // it; a disabled account could not follow a link.
                      disabled={one.id === me.id || Boolean(one.disabled_at)}
                      title={
                        one.id === me.id
                          ? "Change your own password or authenticator from your profile"
                          : one.disabled_at
                            ? "Re-enable them first: a disabled account cannot follow a link"
                            : undefined
                      }
                      onClick={() => setResetting(one)}
                    >
                      Reset sign-in…
                    </button>
                    <button
                      className={one.disabled_at ? "link" : "link danger"}
                      aria-label={`${one.disabled_at ? "Re-enable" : "Disable"} ${one.display_name}`}
                      disabled={one.id === me.id && !one.disabled_at}
                      title={
                        one.id === me.id && !one.disabled_at
                          ? "You cannot disable yourself"
                          : undefined
                      }
                      onClick={() =>
                        one.disabled_at
                          ? setDisabled.mutate({ id: one.id, disabled: false })
                          : setConfirming({ user: one, disable: true })
                      }
                    >
                      {one.disabled_at ? "Re-enable" : "Disable"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small" style={{ marginTop: 12 }}>
        Disabling somebody signs them out everywhere and forgets their trusted browsers. It leaves
        their work alone — their name stays on every batch they ran.
      </p>

      {confirming && "disable" in confirming ? (
        <Dialog
          title={`Disable ${confirming.user.display_name}?`}
          onClose={() => setConfirming(null)}
        >
          <p style={{ marginTop: 0 }}>
            {confirming.user.display_name} ({confirming.user.email}) is signed out everywhere and
            their trusted browsers are forgotten. Re-enable brings the account back.
          </p>
          <div className="dialog-choices">
            <button
              className="danger"
              disabled={setDisabled.isPending}
              onClick={() => setDisabled.mutate({ id: confirming.user.id, disabled: true })}
            >
              {setDisabled.isPending ? "Disabling…" : "Yes, disable them"}
            </button>
            <button disabled={setDisabled.isPending} onClick={() => setConfirming(null)}>
              Keep them active
            </button>
          </div>
          <Problem error={setDisabled.error} />
        </Dialog>
      ) : confirming ? (
        <Dialog
          title={`Make ${confirming.user.display_name} ${confirming.role === "owner" ? "an owner" : "a member"}?`}
          onClose={() => setConfirming(null)}
        >
          <p style={{ marginTop: 0 }}>
            {confirming.role === "owner"
              ? `${confirming.user.display_name} will be able to administer this instance: people, households and invitations.`
              : `${confirming.user.display_name} will no longer be able to administer this instance.`}
          </p>
          <div className="dialog-choices">
            <button
              className="primary"
              disabled={setRole.isPending}
              onClick={() => setRole.mutate({ id: confirming.user.id, role: confirming.role })}
            >
              {setRole.isPending
                ? "Changing…"
                : `Yes, make them ${confirming.role === "owner" ? "an owner" : "a member"}`}
            </button>
            <button disabled={setRole.isPending} onClick={() => setConfirming(null)}>
              Leave it
            </button>
          </div>
          <Problem error={setRole.error} />
        </Dialog>
      ) : null}
      {promoting && (
        <PromoteToOwner
          person={promoting}
          onClose={() => setPromoting(null)}
          onDone={() => {
            setPromoting(null);
            refresh();
          }}
        />
      )}
      {resetting && (
        <ResetSignIn
          person={resetting}
          pending={(pending.data ?? []).find((one) => one.user_id === resetting.id)}
          onClose={() => setResetting(null)}
          // At once, not when the dialog closes: the account is already shut.
          onIssued={refresh}
        />
      )}
    </div>
  );
}

/**
 * Making a member an owner, with both factors bought and spent in one click.
 * The grant never sits in the page: see `components/StepUp.tsx`.
 */
function PromoteToOwner({
  person,
  onClose,
  onDone,
}: {
  person: AdminUser;
  onClose: () => void;
  onDone: () => void;
}) {
  const [proof, setProof] = useState<StepUpProof>(NO_PROOF);
  const promote = useMutation({
    mutationFn: async () => {
      const token = await stepUpToken(proof);
      return api.post<AdminUser>(`/admin/users/${person.id}/role`, {
        role: "owner",
        step_up_token: token,
      });
    },
    onSuccess: onDone,
    // The code bought whatever grant it could; a retry needs the next one.
    onError: () => setProof((current) => ({ ...current, code: "" })),
  });

  return (
    <Dialog title={`Make ${person.display_name} an owner?`} onClose={onClose}>
      <Problem error={promote.error} />
      <StepUpFields
        proof={proof}
        onChange={setProof}
        why="An owner can administer this instance, and stays one whatever you later do to your own password or authenticator."
      />
      <div className="dialog-choices">
        <button
          className="primary"
          disabled={!stepUpReady(proof) || promote.isPending}
          onClick={() => promote.mutate()}
        >
          {promote.isPending ? "Making them an owner…" : "Make them an owner"}
        </button>
        <button disabled={promote.isPending} onClick={onClose}>
          Cancel
        </button>
      </div>
    </Dialog>
  );
}

/**
 * Resetting somebody's sign-in (#286): which of the two, what happens at
 * once, and then the link, shown once.
 *
 * Nothing waits for the link to be followed. The account is shut the moment
 * this is confirmed -- signed out everywhere, the password or the
 * authenticator gone -- so the dialog says so before the button, not after.
 */
function ResetSignIn({
  person,
  pending,
  onClose,
  onIssued,
}: {
  person: AdminUser;
  /** Their link already outstanding, if any: the new one replaces it. */
  pending: PendingReset | undefined;
  onClose: () => void;
  onIssued: () => void;
}) {
  const [password, setPassword] = useState(false);
  const [authenticator, setAuthenticator] = useState(false);
  const [issued, setIssued] = useState<ResetIssued | null>(null);
  const [copied, setCopied] = useState(false);
  // From the click until the answer is in, nothing closes this dialog and
  // nothing sends a second request. The server shuts the account before it
  // answers, and the answer carries the only copy of the link: closed in
  // between, the dialog would unmount, the link would land on nothing, and it
  // would be shown zero times. Sent twice, the second link replaces the first,
  // and whichever answer came back last is the one shown. A ref, set in the
  // click itself, because `issue.isPending` reaches the render a tick after
  // the click.
  const sending = useRef(false);
  const issue = useMutation({
    mutationFn: () =>
      api.post<ResetIssued>(`/admin/users/${person.id}/reset`, { password, authenticator }),
    onSuccess: (made) => {
      setIssued(made);
      onIssued();
    },
    onSettled: () => {
      sending.current = false;
    },
  });
  const send = () => {
    if (sending.current) return;
    sending.current = true;
    issue.mutate();
  };
  // Escape, the ✕ and a click outside all come here.
  const closeUnlessSending = () => {
    if (!sending.current) onClose();
  };
  const chosen = password || authenticator;
  const name = person.display_name;

  if (issued) {
    // Escape and the ✕ ask the question Done asks: the link is in this
    // dialog and nowhere else.
    const leave = () => {
      if (copied || window.confirm("Close without copying? The link cannot be shown again.")) {
        onClose();
      }
    };
    return (
      <Dialog title={`Reset link for ${name}`} onClose={leave}>
        <p style={{ marginTop: 0 }}>
          {name} is signed out everywhere and cannot sign in until they follow this link. It sets
          a new {switchesText(issued.password, issued.authenticator)}.
        </p>
        {(issued.password !== password || issued.authenticator !== authenticator) && (
          <p className="small muted">
            That is more than you chose: what was already reset for them stays reset, so the link
            has to set it too.
          </p>
        )}
        <FreshLink
          link={issued.link}
          label="Reset link"
          onDismiss={onClose}
          onCopied={() => setCopied(true)}
        >
          <div className="small" style={{ marginTop: 6 }}>
            Expires {formatInstant(issued.expires_at)}. Hand it over yourself; it is not sent
            anywhere.
          </div>
        </FreshLink>
      </Dialog>
    );
  }

  const consequences = [
    "every session, trusted browser, agent key and passkey ends",
    password && "their password stops working",
    authenticator && "their authenticator and recovery codes are cleared",
  ].filter(Boolean);

  return (
    <Dialog title={`Reset sign-in for ${name}?`} onClose={closeUnlessSending}>
      <Problem error={issue.error} />
      <p style={{ marginTop: 0 }}>
        Choose what {name} sets again. They do it themselves, from a one-time link you hand over.
      </p>
      <label className="small" style={{ display: "block", marginBottom: 4 }}>
        <input
          type="checkbox"
          checked={password}
          onChange={(e) => setPassword(e.target.checked)}
          style={{ width: "auto", marginRight: 8 }}
        />
        Password
      </label>
      <label className="small" style={{ display: "block", marginBottom: 4 }}>
        <input
          type="checkbox"
          checked={authenticator}
          onChange={(e) => setAuthenticator(e.target.checked)}
          style={{ width: "auto", marginRight: 8 }}
        />
        Authenticator, with its recovery codes
      </label>
      {chosen ? (
        <p className="small">
          <strong>At once, not when the link is followed:</strong> {name} is signed out
          everywhere — {consequences.join("; ")}. They cannot sign in again until they follow the
          link.
        </p>
      ) : (
        <p className="small muted">Choose at least one.</p>
      )}
      {pending && (
        <p className="small muted">
          A reset link for {name} is already pending. This one replaces it, and what that one reset
          stays reset.
        </p>
      )}
      <p className="small muted">
        {name} is told on the link page that you reset them, and every owner sees it under Recent
        sign-in changes.
      </p>
      <div className="dialog-choices">
        <button
          className="danger"
          disabled={!chosen || issue.isPending}
          onClick={send}
        >
          {issue.isPending ? "Resetting…" : "Reset and make the link"}
        </button>
        <button disabled={issue.isPending} onClick={closeUnlessSending}>
          Cancel
        </button>
      </div>
    </Dialog>
  );
}

/**
 * Links handed out and not yet followed. A lapsed one stays listed, marked,
 * because the account it was for is still shut: the list is where an owner
 * finds out a second link is owed.
 */
function PendingResets() {
  const client = useQueryClient();
  const pending = useQuery({ queryKey: PENDING_KEY, queryFn: readPending });
  const resets = pending.data ?? [];
  const [withdrawing, setWithdrawing] = useState<PendingReset | null>(null);
  const withdraw = useMutation({
    mutationFn: (id: string) => api.del(`/admin/resets/${id}`),
    onSuccess: () => {
      setWithdrawing(null);
      // The audit log behind Recent sign-in changes moved too.
      return client.invalidateQueries({ queryKey: ["admin"] });
    },
  });

  const order = useSort<PendingSort>("date", "desc");
  const rows = useMemo(
    () =>
      sortRows(
        resets,
        order.sort,
        order.direction,
        (one, column) => {
          switch (column) {
            case "who":
              return one.display_name;
            case "resets":
              return switchesText(one.password, one.authenticator);
            case "by":
              return one.issued_by ?? "from the server";
            case "expires":
              return one.expires_at;
            default:
              return one.created_at;
          }
        },
        (a, b) => b.created_at.localeCompare(a.created_at),
      ),
    [resets, order.sort, order.direction],
  );

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2 style={{ marginTop: 0 }}>Pending reset links</h2>
      <Problem error={pending.error ?? (withdrawing ? null : withdraw.error)} />
      {resets.length === 0 ? (
        <Empty>No reset links pending.</Empty>
      ) : (
        <div className="table-scroll">
          <table aria-label="Pending reset links">
            <thead>
              <tr>
                <SortHeading
                  label="Who"
                  column="who"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="Resets"
                  column="resets"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="Issued by"
                  column="by"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="Issued"
                  column="date"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="Expires"
                  column="expires"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((one) => (
                <tr key={one.id}>
                  <td>
                    {one.display_name} <span className="small muted">{one.email}</span>
                  </td>
                  <td className="small">{switchesText(one.password, one.authenticator)}</td>
                  <td className="small">
                    {one.issued_by ?? <span className="muted">from the server</span>}
                  </td>
                  <td className="small muted">{formatInstant(one.created_at)}</td>
                  <td className="small">
                    {one.expired ? (
                      <>
                        <span className="neg">expired</span>{" "}
                        <span className="muted">{formatInstant(one.expires_at)}</span>
                      </>
                    ) : (
                      <span className="muted">{formatInstant(one.expires_at)}</span>
                    )}
                  </td>
                  <td>
                    <button
                      className="link danger"
                      aria-label={`Withdraw the reset link for ${one.display_name}`}
                      onClick={() => setWithdrawing(one)}
                    >
                      Withdraw
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small" style={{ marginTop: 12 }}>
        Withdrawing a link stops it working. It does not undo the reset: the account stays shut,
        and the way back in is a new link.
      </p>

      {withdrawing && (
        <Dialog title="Withdraw this reset link?" onClose={() => setWithdrawing(null)}>
          <p style={{ marginTop: 0 }}>
            The link for {withdrawing.display_name} ({withdrawing.email}) stops working at once.
            Their {switchesText(withdrawing.password, withdrawing.authenticator)} stays reset, so
            they cannot sign in until you make them a new link.
          </p>
          <div className="dialog-choices">
            <button
              className="danger"
              disabled={withdraw.isPending}
              onClick={() => withdraw.mutate(withdrawing.id)}
            >
              {withdraw.isPending ? "Withdrawing…" : "Yes, withdraw it"}
            </button>
            <button disabled={withdraw.isPending} onClick={() => setWithdrawing(null)}>
              Keep it
            </button>
          </div>
          <Problem error={withdraw.error} />
        </Dialog>
      )}
    </div>
  );
}

/**
 * Resets, new owners and what the server did to an account's way in, in the
 * last fourteen days, read by the server from the audit log. Yours included:
 * this is the record, and the notice in the shell is the part that interrupts.
 */
function RecentSignInChanges() {
  // The shell's notice reads the same key, so one fetch serves both.
  const recent = useQuery({
    queryKey: ["admin", "sign-in-changes"],
    queryFn: () => api.get<SignInChange[]>("/admin/sign-in-changes"),
  });
  const changes = recent.data ?? [];
  const order = useSort<ChangeSort>("date", "desc");
  const rows = useMemo(
    () =>
      sortRows(
        changes,
        order.sort,
        order.direction,
        (one, column) => {
          switch (column) {
            case "what":
              return whatText(one);
            case "whom":
              return one.user_name;
            case "by":
              return byText(one);
            default:
              return one.at;
          }
        },
        // The log's own order: newer is a larger position.
        (a, b) => b.id - a.id || b.key.localeCompare(a.key),
      ),
    [changes, order.sort, order.direction],
  );

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2 style={{ marginTop: 0 }}>Recent sign-in changes</h2>
      <p className="muted small" style={{ marginTop: 0 }}>
        Every reset link, every new owner, and anything the server did to an account's way in,
        in the last 14 days, by anyone. Read from the audit log.
      </p>
      <Problem error={recent.error} />
      {changes.length === 0 ? (
        <Empty>Nothing in the last 14 days.</Empty>
      ) : (
        <div className="table-scroll">
          <table aria-label="Recent sign-in changes">
            <thead>
              <tr>
                <SortHeading
                  label="When"
                  column="date"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="What"
                  column="what"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="Whom"
                  column="whom"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="By"
                  column="by"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
              </tr>
            </thead>
            <tbody>
              {rows.map((one) => (
                <tr key={one.key}>
                  <td className="small muted">{formatInstant(one.at)}</td>
                  <td className="small">{whatText(one)}</td>
                  <td>{one.user_name}</td>
                  <td className="small">
                    {one.from_server ? <span className="muted">{byText(one)}</span> : byText(one)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Households
// --------------------------------------------------------------------------- //

function Households({
  households,
  users,
}: {
  households: AdminHousehold[];
  users: AdminUser[];
}) {
  const client = useQueryClient();
  const [adding, setAdding] = useState(false);

  const order = useSort<HouseholdSort>("name");
  const rows = useMemo(
    () =>
      sortRows(
        households,
        order.sort,
        order.direction,
        (one, column) => {
          switch (column) {
            case "currency":
              return one.base_currency;
            case "dates":
              return one.date_format;
            case "members":
              return one.member_ids.length;
            default:
              return one.name;
          }
        },
        (a, b) => compareNames(a.name, b.name),
      ),
    [households, order.sort, order.direction],
  );

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
        <p className="muted small" style={{ margin: 0 }}>
          Every household on this instance, including ones you are not in. Joining one is still an
          explicit act.
        </p>
        <button className="primary" onClick={() => setAdding(true)}>
          New household
        </button>
      </div>

      <div className="card">
        {households.length === 0 ? (
          <Empty>No households yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <SortHeading
                    label="Name"
                    column="name"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                  />
                  <SortHeading
                    label="Main currency"
                    column="currency"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                  />
                  <SortHeading
                    label="Dates"
                    column="dates"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                  />
                  <SortHeading
                    label="Members"
                    column="members"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                  />
                </tr>
              </thead>
              <tbody>
                {rows.map((one) => (
                  <tr key={one.id}>
                    <td>{one.name}</td>
                    <td className="mono small">{one.base_currency}</td>
                    <td className="mono small muted">{one.date_format}</td>
                    <td className="small muted">
                      {one.member_ids.length === 0
                        ? "nobody"
                        : one.member_ids
                            .map(
                              (id) =>
                                users.find((u) => u.id === id)?.display_name ?? "somebody",
                            )
                            .join(", ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {adding && (
        <NewHousehold
          users={users}
          onClose={() => setAdding(false)}
          onCreated={() => {
            client.invalidateQueries({ queryKey: ["admin"] });
            client.invalidateQueries({ queryKey: ["households"] });
            setAdding(false);
          }}
        />
      )}
    </>
  );
}

function NewHousehold({
  users,
  onClose,
  onCreated,
}: {
  users: AdminUser[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [currency, setCurrency] = useState("EUR");
  const [dateFormat, setDateFormat] = useState("YYYY-MM-DD");
  const [members, setMembers] = useState<string[]>([]);

  const create = useMutation({
    mutationFn: () =>
      api.post<AdminHousehold>("/admin/households", {
        name,
        base_currency: currency.toUpperCase(),
        date_format: dateFormat,
        member_ids: members,
      }),
    onSuccess: onCreated,
  });

  const toggle = (id: string) =>
    setMembers((current) =>
      current.includes(id) ? current.filter((one) => one !== id) : [...current, id],
    );

  return (
    <Panel title="New household" onClose={onClose} config>
      <Problem error={create.error} />
      <Field label="Name">
        <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </Field>
      <p />
      <Field label="Main currency">
        <input
          value={currency}
          onChange={(e) => setCurrency(e.target.value)}
          maxLength={3}
          spellCheck={false}
        />
      </Field>
      <p className="muted small">
        Used for totals only. Each account keeps its own currency, and nothing is ever converted.
      </p>
      <Field label="Date format">
        <select value={dateFormat} onChange={(e) => setDateFormat(e.target.value)}>
          <option value="YYYY-MM-DD">YYYY-MM-DD</option>
          <option value="DD/MM/YYYY">DD/MM/YYYY</option>
          <option value="MM/DD/YYYY">MM/DD/YYYY</option>
        </select>
      </Field>
      <p />
      <div className="field">
        <span style={{ fontWeight: 600, fontSize: 13, color: "var(--muted)" }}>Who is in it</span>
        <div style={{ marginTop: 6 }}>
          {users
            .filter((one) => !one.disabled_at)
            .map((one) => (
              <label key={one.id} className="small" style={{ display: "block", marginBottom: 4 }}>
                <input
                  type="checkbox"
                  checked={members.includes(one.id)}
                  onChange={() => toggle(one.id)}
                  style={{ width: "auto", marginRight: 8 }}
                />
                {one.display_name} <span className="muted">{one.email}</span>
              </label>
            ))}
        </div>
      </div>
      <p className="muted small">You are a member of anything you create, whether or not you tick yourself.</p>
      <button
        className="primary"
        disabled={create.isPending || !name.trim() || currency.trim().length !== 3}
        onClick={() => create.mutate()}
      >
        Create household
      </button>
    </Panel>
  );
}

// --------------------------------------------------------------------------- //
// Invitations
// --------------------------------------------------------------------------- //

function Invitations({ households }: { households: AdminHousehold[] }) {
  const client = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [fresh, setFresh] = useState<InviteCreated | null>(null);

  const invitations = useQuery({
    queryKey: ["admin", "invitations"],
    queryFn: () => api.get<Invitation[]>("/admin/invitations"),
  });

  //: Asked before it is sent (#199): a revoked link cannot be revived.
  const [revoking, setRevoking] = useState<Invitation | null>(null);
  const revoke = useMutation({
    mutationFn: (id: string) => api.del(`/admin/invitations/${id}`),
    onSuccess: () => {
      setRevoking(null);
      return client.invalidateQueries({ queryKey: ["admin", "invitations"] });
    },
  });

  const householdName = (id: string) => households.find((h) => h.id === id)?.name ?? id;

  // Soonest to expire first: an invitation is a thing with a deadline, and the
  // one about to lapse is the one worth knowing about.
  const order = useSort<InviteSort>("expires");
  const rows = useMemo(
    () =>
      sortRows(
        invitations.data ?? [],
        order.sort,
        order.direction,
        (one, column) => {
          switch (column) {
            // An open link has no address; those sort last, as blanks do.
            case "for":
              return one.email;
            case "role":
              return one.role;
            case "households":
              return [
                (one.household_ids ?? []).length,
                (one.household_ids ?? []).map(householdName).join(", "),
              ];
            default:
              return one.expires_at;
          }
        },
        (a, b) => a.expires_at.localeCompare(b.expires_at),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [invitations.data, households, order.sort, order.direction],
  );

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
        <p className="muted small" style={{ margin: 0 }}>
          A one-time link. The role travels on it, so you can hand somebody ownership without ever
          holding their password.
        </p>
        <button className="primary" onClick={() => setCreating(true)}>
          Invite somebody
        </button>
      </div>

      {fresh && (
        <FreshLink link={fresh.link} label="Invitation link" onDismiss={() => setFresh(null)} />
      )}

      <Problem error={invitations.error ?? (revoking ? null : revoke.error)} />

      <div className="card">
        {(invitations.data ?? []).length === 0 ? (
          <Empty>No invitations outstanding.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <SortHeading
                    label="For"
                    column="for"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                  />
                  <SortHeading
                    label="Role"
                    column="role"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                  />
                  <SortHeading
                    label="Households"
                    column="households"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                  />
                  <SortHeading
                    label="Expires"
                    column="expires"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                  />
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((one) => (
                  <tr key={one.id}>
                    <td className="small">{one.email ?? <span className="muted">anyone with the link</span>}</td>
                    <td className="small">{roleLabel(one.role)}</td>
                    <td className="small muted">
                      {(one.household_ids ?? []).length === 0
                        ? "none yet"
                        : (one.household_ids ?? []).map(householdName).join(", ")}
                    </td>
                    <td className="small muted">{formatInstant(one.expires_at)}</td>
                    <td>
                      <button className="link danger" onClick={() => setRevoking(one)}>
                        Revoke
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {revoking && (
        <Dialog title="Revoke this invitation?" onClose={() => setRevoking(null)}>
          <p style={{ marginTop: 0 }}>
            The link for {revoking.email ?? "anyone with the link"} ({roleLabel(revoking.role)}) stops working
            at once. To invite them again, make a new one.
          </p>
          <div className="dialog-choices">
            <button
              className="danger"
              disabled={revoke.isPending}
              onClick={() => revoke.mutate(revoking.id)}
            >
              {revoke.isPending ? "Revoking…" : "Yes, revoke it"}
            </button>
            <button disabled={revoke.isPending} onClick={() => setRevoking(null)}>
              Keep it
            </button>
          </div>
          <Problem error={revoke.error} />
        </Dialog>
      )}

      {creating && (
        <NewInvitation
          households={households}
          onClose={() => setCreating(false)}
          onCreated={(created) => {
            setFresh(created);
            client.invalidateQueries({ queryKey: ["admin", "invitations"] });
            setCreating(false);
          }}
        />
      )}
    </>
  );
}

/**
 * The one showing of a one-time link.
 *
 * The server keeps only a hash, so if this is dismissed before the link is
 * somewhere safe the only recovery is to revoke and start again. Hence: the
 * copy either reports success or tells you to do it by hand, and Done asks.
 */
function FreshLink({
  link,
  label,
  onDismiss,
  onCopied,
  children,
}: {
  link: string;
  /** What the field is called: "Invitation link", "Reset link". */
  label: string;
  onDismiss: () => void;
  /** For a holder that has its own way to close, so it can ask the same question. */
  onCopied?: () => void;
  /** Anything else worth saying beside this link in particular. */
  children?: ReactNode;
}) {
  const [copied, setCopied] = useState<"no" | "yes" | "failed">("no");

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(link);
      setCopied("yes");
      onCopied?.();
    } catch {
      // No Clipboard API, a denied permission, or an unfocused document. The
      // promise rejects silently otherwise and Done then destroys the link.
      setCopied("failed");
    }
  };

  return (
    <div className="banner info">
      <strong>Copy this link now — it is shown once.</strong>
      <div className="row" style={{ marginTop: 8 }}>
        <input
          className="mono"
          readOnly
          aria-label={label}
          value={link}
          onFocus={(e) => e.target.select()}
        />
        <button onClick={copy}>{copied === "yes" ? "Copied" : "Copy"}</button>
        <button
          onClick={() => {
            if (copied === "yes" || window.confirm("Dismiss without copying? The link cannot be shown again.")) {
              onDismiss();
            }
          }}
        >
          Done
        </button>
      </div>
      {copied === "failed" && (
        <div className="small neg" style={{ marginTop: 6 }}>
          Couldn't reach the clipboard. Select the link above and copy it by hand.
        </div>
      )}
      {children}
      <div className="small muted" style={{ marginTop: 6 }}>
        The server keeps only a hash of it, so nobody — you included — can read it back.
      </div>
    </div>
  );
}

function NewInvitation({
  households,
  onClose,
  onCreated,
}: {
  households: AdminHousehold[];
  onClose: () => void;
  onCreated: (created: InviteCreated) => void;
}) {
  const [role, setRole] = useState<Role>("member");
  const [email, setEmail] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [proof, setProof] = useState<StepUpProof>(NO_PROOF);

  const create = useMutation({
    mutationFn: async () =>
      api.post<InviteCreated>("/admin/invitations", {
        role,
        email: email.trim() || null,
        household_ids: chosen,
        // Only an owner-role link costs both factors; the grant is bought
        // here, inside the click that spends it.
        step_up_token: role === "owner" ? await stepUpToken(proof) : null,
      }),
    onSuccess: onCreated,
    onError: () => setProof((current) => ({ ...current, code: "" })),
  });
  const ready = role !== "owner" || stepUpReady(proof);

  const toggle = (id: string) =>
    setChosen((current) =>
      current.includes(id) ? current.filter((one) => one !== id) : [...current, id],
    );

  return (
    <Panel title="Invite somebody" onClose={onClose} config>
      <Problem error={create.error} />
      <Field label="Role">
        <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
          <option value="member">Member</option>
          <option value="owner">Owner — can administer this instance</option>
        </select>
      </Field>
      <p />
      <Field label="Email (optional)">
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="fills in the acceptance screen"
        />
      </Field>
      <p className="muted small">
        A suggestion, not a restriction: it fills in the address on the acceptance screen and the
        person can change it. <strong>The link is the credential</strong> — whoever opens it gets
        this role, so treat it like a password.
      </p>
      <div className="field">
        <span style={{ fontWeight: 600, fontSize: 13, color: "var(--muted)" }}>
          Households they join on arrival
        </span>
        <div style={{ marginTop: 6 }}>
          {households.length === 0 ? (
            <span className="muted small">None yet.</span>
          ) : (
            households.map((one) => (
              <label key={one.id} className="small" style={{ display: "block", marginBottom: 4 }}>
                <input
                  type="checkbox"
                  checked={chosen.includes(one.id)}
                  onChange={() => toggle(one.id)}
                  style={{ width: "auto", marginRight: 8 }}
                />
                {one.name}
              </label>
            ))
          )}
        </div>
      </div>
      <p />
      {role === "owner" && (
        <>
          <StepUpFields
            proof={proof}
            onChange={setProof}
            why="Whoever opens an owner link can administer this instance, and stays an owner whatever you later do to your own password or authenticator."
          />
          <p />
        </>
      )}
      <button
        className="primary"
        disabled={!ready || create.isPending}
        onClick={() => create.mutate()}
      >
        Create the link
      </button>
    </Panel>
  );
}
