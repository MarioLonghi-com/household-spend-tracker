/**
 * Notice a change made somewhere else, and refresh what is on screen (#369).
 *
 * `main.tsx` keeps `refetchOnWindowFocus: false` -- a household ledger is not
 * a live feed, and refetching every query because a window got focus is
 * noise. This keeps that intent: nothing is refetched until the household's
 * audit log says something changed. It asks one small question,
 * `GET /households/{id}/changes/latest?since=N`, which the server answers
 * from the newest `changes.seq` without writing anything -- not even the
 * session's last-seen time, so a tab left open neither turns into a stream
 * of WAL writes nor keeps an idle session alive.
 *
 * - **Only while the tab is visible.** Hidden, the timer stops; shown again,
 *   it asks at once and starts over at the base interval.
 * - **Backing off when idle**, from 20 seconds to two minutes, and back to 20
 *   the moment something changes -- #340 was a poll that never backed off.
 * - **Its own changes are already in hand.** The server says whether what
 *   changed was this person's, in a browser, or somebody else's (another
 *   member, or an agent). This person's change is taken as this tab's own
 *   when this tab saved something since it last asked; otherwise it was
 *   another of their tabs, and refreshes like any other.
 * - **What it does on a change:** invalidates this household's queries, so
 *   the ones on screen refetch in the background and keep what they show
 *   until the answer arrives -- no reset scroll, the windowed register keeps
 *   its place, and a half-typed field keeps its text. The register's panel
 *   decides for itself what to do when its own row changed underneath.
 */

import { useEffect, useRef, useState } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "./api";

/** The first interval, and the one a change or a return to the tab resets to. */
export const BASE_MS = 20_000;
/** The longest an idle tab waits between two questions. */
export const MAX_MS = 120_000;

export type Latest = { seq: number; elsewhere: boolean; mine: boolean };

/** How long to wait after a poll: back to the base on a change, else longer. */
export function nextDelay(current: number, changed: boolean): number {
  if (changed) return BASE_MS;
  return Math.min(MAX_MS, Math.round(current * 1.5));
}

/**
 * Whether what the server answered should refresh this tab.
 *
 * Somebody else's change always does. This person's own does unless this tab
 * saved something since it last asked -- then it is this tab's, and already
 * on screen. Nothing new (`seq` not past the baseline) never does.
 */
export function shouldRefresh(answer: Latest, baseline: number, wroteHere: boolean): boolean {
  if (answer.seq <= baseline) return false;
  if (answer.elsewhere) return true;
  return answer.mine && !wroteHere;
}

/** Refetch what this household's screens hold, in the background. */
export function refreshHousehold(client: QueryClient, householdId: string): Promise<void> {
  return client.invalidateQueries({
    predicate: (query) =>
      query.queryKey[0] !== "household-changes" && query.queryKey.includes(householdId),
  });
}

/**
 * Listen for changes to one household. Returns when the screen was last
 * refreshed because of one, for the "Updated just now" notice.
 */
export function useLedgerListener(householdId: string | undefined): Date | null {
  const client = useQueryClient();
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null);
  const wroteHere = useRef(false);

  // This tab's own writes: any mutation that succeeded since the last poll.
  useEffect(
    () =>
      client.getMutationCache().subscribe((event) => {
        if (event.type === "updated" && event.mutation.state.status === "success") {
          wroteHere.current = true;
        }
      }),
    [client],
  );

  useEffect(() => {
    if (!householdId) return;
    let baseline: number | null = null;
    let delay = BASE_MS;
    let timer: number | undefined;
    let stopped = false;

    const schedule = () => {
      window.clearTimeout(timer);
      if (stopped || document.visibilityState !== "visible") return;
      timer = window.setTimeout(poll, delay);
    };

    async function poll() {
      // A save still in flight would be counted as somebody else's.
      if (client.isMutating() > 0) {
        schedule();
        return;
      }
      try {
        const query = baseline === null ? "" : `?since=${baseline}`;
        const answer = await api.get<Latest>(
          `/households/${householdId}/changes/latest${query}`,
        );
        if (stopped) return;
        const first = baseline === null;
        const changed = !first && shouldRefresh(answer, baseline!, wroteHere.current);
        baseline = answer.seq;
        wroteHere.current = false;
        if (changed) {
          setRefreshedAt(new Date());
          void refreshHousehold(client, householdId!);
        }
        // Taking the baseline is not an idle poll: the first real question
        // still comes one base interval later.
        if (!first) delay = nextDelay(delay, changed);
      } catch {
        // Signed out, offline, or the server restarting: ask again later,
        // more slowly, and say nothing -- other parts of the page say those.
        delay = nextDelay(delay, false);
      }
      schedule();
    }

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        delay = BASE_MS;
        void poll();
      } else {
        window.clearTimeout(timer);
      }
    };

    document.addEventListener("visibilitychange", onVisibility);
    // The baseline straight away, even in a tab opened in the background:
    // a change made before it is first looked at is then noticed when it is,
    // rather than taken as where things started.
    void poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [client, householdId]);

  return refreshedAt;
}
