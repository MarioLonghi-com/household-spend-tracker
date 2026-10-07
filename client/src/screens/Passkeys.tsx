/**
 * Your passkeys, inside Sign-in methods (#122, from #47 §3).
 *
 * One row each: its name, whether it is synced or on this device only, when
 * it was made and last used, and "this device" on the one this browser signed
 * in with. Sorts at its headers like every list here; the action cell is the
 * exception the standing rule allows. A passkey made for another host name --
 * a renamed machine, a restore elsewhere -- says so and can only be removed.
 *
 * Adding is one button and one flow: the step-up (password and code) is asked
 * here, inline, and the browser's passkey prompt follows straight away. The
 * new row appears highlighted with its name ready to edit. Where passkeys
 * cannot work there is no Add button, only one line saying why.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Field, Problem, SortHeading, sortRows, useSort } from "../components/bits";
import { NO_PROOF, spent, stepUpReady, StepUpFields, stepUpToken, type StepUpProof } from "../components/StepUp";
import {
  createPasskey,
  forgetRecoveryReminder,
  type PasskeyState,
  passkeyState,
  recoveryReminder,
  thisDevicePasskey,
  wasDismissed,
} from "../lib/passkeys";
import { formatInstant } from "../lib/time";
import type { Passkey } from "../lib/types";

type Column = "label" | "kind" | "created" | "used";

/** The one line that replaces the Add button, in words a member can act on. */
export function whyNotHere(state: PasskeyState): string {
  switch (state.reason) {
    case "browser":
      return "This browser does not support passkeys yet.";
    case "wrong_host":
    case "insecure":
    case "ip_address":
      return state.address
        ? `Passkeys need this app opened at its HTTPS address, ${state.address}.`
        : "Passkeys need this app opened at its HTTPS address.";
    default:
      return "Passkeys are not set up on this server.";
  }
}

export function usePasskeys() {
  return useQuery({ queryKey: ["passkeys"], queryFn: () => api.get<Passkey[]>("/me/passkeys") });
}

export function usePasskeyState() {
  return useQuery({ queryKey: ["passkey-state"], queryFn: passkeyState, staleTime: Infinity });
}

/** What is left to sign in with once this one is gone, for the question. */
function whatIsLeft(passkeys: Passkey[], removing: Passkey): string {
  const others = passkeys.filter((one) => one.id !== removing.id && one.usable_here).length;
  return others > 0
    ? `You will still be able to sign in with your other ${others === 1 ? "passkey" : `${others} passkeys`}, or with your password and code.`
    : "You will still be able to sign in with your password and code.";
}

export function PasskeysSection() {
  const queries = useQueryClient();
  const listed = usePasskeys();
  const state = usePasskeyState();
  const { sort, direction, onSort } = useSort<Column | "newest">("newest");
  const [adding, setAdding] = useState(false);
  const [proof, setProof] = useState<StepUpProof>(NO_PROOF);
  const [renaming, setRenaming] = useState<{ id: string; label: string } | null>(null);
  const [removing, setRemoving] = useState<Passkey | null>(null);
  const [fresh, setFresh] = useState<string | null>(null);
  const [reminder, setReminder] = useState(recoveryReminder);
  const here = thisDevicePasskey();

  const refresh = () => queries.invalidateQueries({ queryKey: ["passkeys"] });

  const add = useMutation({
    mutationFn: async () => {
      // The grant is bought and spent inside this one click, as issuing a key
      // does, so it never sits in the page.
      const token = await stepUpToken(proof);
      setProof(spent(proof));
      const options = await api.post<unknown>("/me/passkeys/options", { step_up_token: token });
      const credential = await createPasskey(options);
      return api.post<Passkey>("/me/passkeys", { credential });
    },
    onSuccess: (made) => {
      setAdding(false);
      setProof(NO_PROOF);
      setFresh(made.id);
      setRenaming({ id: made.id, label: made.label });
      void refresh();
    },
  });

  const rename = useMutation({
    mutationFn: ({ id, label }: { id: string; label: string }) =>
      api.patch<Passkey>(`/me/passkeys/${id}`, { label }),
    onSuccess: () => {
      setRenaming(null);
      void refresh();
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.del(`/me/passkeys/${id}`),
    onSuccess: () => {
      setRemoving(null);
      void refresh();
    },
  });

  const passkeys = listed.data ?? [];
  const rows = useMemo(
    () =>
      sortRows(
        passkeys,
        sort,
        direction,
        (one, column) => {
          switch (column) {
            case "label":
              return one.label.toLowerCase();
            case "kind":
              return one.synced ? 0 : 1;
            case "created":
              return one.created_at;
            case "used":
              return one.last_used_at ?? "";
            default:
              return one.created_at;
          }
        },
        (a, b) => (a.created_at < b.created_at ? 1 : -1),
      ),
    [passkeys, sort, direction],
  );
  // The list opens newest first, the server's order, with no arrow lit.
  const shown = sort === "newest" ? passkeys : rows;
  const usable = passkeys.filter((one) => one.usable_here).length;
  const available = state.data?.available === true;

  return (
    <div>
      <Problem error={listed.error ?? rename.error ?? remove.error} />

      {reminder > 0 && usable > 0 && (
        <div className="banner warn" role="status">
          You signed in with a recovery code, and your passkeys still work. If one of them was on
          the device you lost, remove it here.{" "}
          <button
            type="button"
            className="link"
            onClick={() => {
              forgetRecoveryReminder();
              setReminder(0);
            }}
          >
            Dismiss
          </button>
        </div>
      )}

      {passkeys.length > 0 && (
        <div className="table-scroll">
          <table className="passkeys">
            <thead>
              <tr>
                <SortHeading label="Name" column="label" sort={sort} direction={direction} onSort={onSort} />
                <SortHeading label="Kept" column="kind" sort={sort} direction={direction} onSort={onSort} />
                <SortHeading label="Added" column="created" sort={sort} direction={direction} onSort={onSort} />
                <SortHeading label="Last used" column="used" sort={sort} direction={direction} onSort={onSort} />
                <th>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {shown.map((one) => (
                <PasskeyRow
                  key={one.id}
                  passkey={one}
                  thisDevice={one.id === here}
                  fresh={one.id === fresh}
                  renaming={renaming?.id === one.id ? renaming.label : null}
                  onRenameStart={() => setRenaming({ id: one.id, label: one.label })}
                  onRenameChange={(label) => setRenaming({ id: one.id, label })}
                  onRenameSave={() => renaming && rename.mutate(renaming)}
                  onRenameCancel={() => setRenaming(null)}
                  onRemove={() => setRemoving(one)}
                  busy={rename.isPending || remove.isPending}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {removing && (
        <div className="card" role="alertdialog" aria-label={`Remove ${removing.label}`} style={{ marginTop: 12 }}>
          <p style={{ marginTop: 0 }}>
            Remove <strong>{removing.label}</strong>? It stops working at once.{" "}
            {removing.usable_here ? whatIsLeft(passkeys, removing) : "It could not be used here anyway."}
          </p>
          <div className="row">
            <button className="danger" disabled={remove.isPending} onClick={() => remove.mutate(removing.id)}>
              Remove
            </button>
            <button onClick={() => setRemoving(null)}>Keep it</button>
          </div>
        </div>
      )}

      {available && usable === 1 && !adding && (
        <p className="small muted">
          Add a second on another device. If you lose this one, you will need your password and
          code.
        </p>
      )}

      {state.data && !available ? (
        <p className="small muted">{whyNotHere(state.data)}</p>
      ) : adding ? (
        <div className="card" style={{ marginTop: 12 }}>
          <Problem error={add.error && !wasDismissed(add.error) ? add.error : null} />
          {add.error && wasDismissed(add.error) ? (
            <p className="small muted">The passkey prompt was closed, so nothing was added.</p>
          ) : null}
          <StepUpFields
            proof={proof}
            onChange={setProof}
            why="A passkey is a way in that keeps working after this browser is closed."
          />
          <div className="row" style={{ marginTop: 12 }}>
            <button className="primary" disabled={!stepUpReady(proof) || add.isPending} onClick={() => add.mutate()}>
              {add.isPending ? "Waiting for the passkey…" : "Continue to the passkey"}
            </button>
            <button
              onClick={() => {
                setAdding(false);
                setProof(NO_PROOF);
                add.reset();
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : available ? (
        <button style={{ marginTop: 12 }} onClick={() => setAdding(true)}>
          Add a passkey
        </button>
      ) : null}
    </div>
  );
}

function PasskeyRow({
  passkey,
  thisDevice,
  fresh,
  renaming,
  onRenameStart,
  onRenameChange,
  onRenameSave,
  onRenameCancel,
  onRemove,
  busy,
}: {
  passkey: Passkey;
  thisDevice: boolean;
  fresh: boolean;
  renaming: string | null;
  onRenameStart: () => void;
  onRenameChange: (label: string) => void;
  onRenameSave: () => void;
  onRenameCancel: () => void;
  onRemove: () => void;
  busy: boolean;
}) {
  const name = useRef<HTMLInputElement>(null);
  const editing = renaming !== null;
  useEffect(() => {
    if (editing) name.current?.select();
  }, [editing]);

  return (
    <tr className={fresh ? "fresh" : undefined}>
      <td>
        {renaming !== null ? (
          <form
            className="row"
            onSubmit={(event) => {
              event.preventDefault();
              onRenameSave();
            }}
          >
            <Field label={`New name for ${passkey.label}`}>
              <input
                ref={name}
                value={renaming}
                maxLength={80}
                autoFocus
                onChange={(e) => onRenameChange(e.target.value)}
              />
            </Field>
            <button type="submit" className="primary" disabled={busy || renaming.trim() === ""}>
              Save
            </button>
            <button type="button" onClick={onRenameCancel}>
              Cancel
            </button>
          </form>
        ) : (
          <>
            <strong>{passkey.label}</strong>
            {thisDevice && <span className="tag">this device</span>}
            {!passkey.usable_here && (
              <div className="small neg">Made for {passkey.rp_id}, cannot be used here.</div>
            )}
          </>
        )}
      </td>
      <td className="small">{passkey.synced ? "Synced" : "This device only"}</td>
      <td className="small">{formatInstant(passkey.created_at)}</td>
      <td className="small">{passkey.last_used_at ? formatInstant(passkey.last_used_at) : "Never"}</td>
      <td className="actions">
        {renaming === null && (
          <div className="row">
            {passkey.usable_here && (
              <button disabled={busy} onClick={onRenameStart} aria-label={`Rename ${passkey.label}`}>
                Rename
              </button>
            )}
            <button className="danger" disabled={busy} onClick={onRemove} aria-label={`Remove ${passkey.label}`}>
              Remove
            </button>
          </div>
        )}
      </td>
    </tr>
  );
}
