// @vitest-environment jsdom

/**
 * The Updates section (#166; design notes 3.1-3.8, tests A5, A7, A8, A9).
 *
 * What is pinned: each of the five cases says its own sentence, with the
 * container and engine named from the heartbeat (A5); release notes are text,
 * never markup (A7); the Update button needs every lossy box **and** the
 * recovery-code box **and** both factors, and the apply names exactly the
 * lossy set and the code it was shown (A8); every outcome renders (A9); the
 * recovery code is asked for with a POST (R21); and the update panel, once
 * the app has gone, polls `/api/health`, says "not back yet" at 30 minutes
 * with the recovery link, and, when the app answers, loads the page again on
 * Application management, where the outcome is.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import {
  APPLY_POLL_MS,
  HEALTH_POLL_MS,
  NOT_BACK_AFTER_MS,
  OutcomeBlock,
  ReleaseNotes,
  Updates,
  Updating,
  type Heartbeat,
  type Outcome,
  type Report,
  type UpdateState,
  type Upstream,
} from "./Updates";
import { UpdateBackupList } from "./Backups";
import type { Backup } from "./Backups";

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);
const del = vi.mocked(api.del);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// --------------------------------------------------------------------------- //
// Fixtures: two of everything -- two newer releases, two lossy migrations,
// two engines, two container names.
// --------------------------------------------------------------------------- //

const DIGEST = "sha256:" + "3f2a".repeat(16);
const UPDATER_DIGEST = "sha256:" + "77b0".repeat(16);

function beat(over: Partial<Heartbeat> = {}): Heartbeat {
  return {
    seen_at: "2026-10-08T10:00:00Z",
    fresh: true,
    updater_version: "0.8.0",
    image_digest: "sha256:" + "1111".repeat(16),
    engine: "docker-desktop",
    engine_version: "4.48.0",
    rootless: false,
    layout: "loopback",
    socket: "ok",
    hook: false,
    busy: false,
    role: "current",
    protocols: "1-1",
    api_version: "1.47",
    engine_api: "1.40-1.51",
    container: "spend-tracker-updater-1",
    socket_sentence: null,
    ...over,
  };
}

function state(over: Partial<UpdateState> = {}): UpdateState {
  return {
    case: "working",
    running: "0.8.0",
    protocol: 1,
    in_flight: false,
    heartbeat: beat(),
    status: null,
    report: null,
    outcome: null,
    backups: [],
    ...over,
  };
}

const UPSTREAM: Upstream = {
  checked_at: "2026-10-08T10:00:00Z",
  running: "0.8.0",
  latest: "0.10.0",
  newer: true,
  problem: null,
  releases: [
    {
      version: "0.10.0",
      tag: "v0.10.0",
      name: null,
      published_at: "2026-10-07T09:00:00Z",
      notes: "Added: receipts by date.",
      notes_from: "changelog",
    },
    {
      version: "0.9.0",
      tag: "v0.9.0",
      name: null,
      published_at: "2026-10-01T09:00:00Z",
      notes: "Changed: theme colours fold into one.",
      notes_from: "changelog",
    },
  ],
  updater: { version: "0.10.0", compatible: null, note: "The updater checks it." },
};

const REPORT: Report = {
  id: "8f6c2f7e-1111-4a2b-9c3d-000000000001",
  from_version: "0.8.0",
  to_version: "0.10.0",
  digest: DIGEST,
  updater_digest: UPDATER_DIGEST,
  expires_at: "2026-10-09T10:00:00Z",
  database_stamp: "2de003489b79",
  pending: [
    { revision: "b2c3d4e5f6a1", title: "Fold theme colours", reversible: "lossy", note: "old values go" },
    { revision: "c3d4e5f6a1b2", title: "Index receipts", reversible: "clean", note: null },
    { revision: "d4e5f6a1b2c3", title: "An old one", reversible: "undeclared", note: null },
  ],
  lossy: ["b2c3d4e5f6a1", "d4e5f6a1b2c3"],
  sizes: {},
  attestations: { app: { commit: "9c41e0b7d2aa", repository: "Example/spend-tracker" } },
};

const CODE = {
  id: "code-1",
  code: "KQ7M-AB12-CD34-EF56-GH78-JK9M-W2PX",
  prepared_id: REPORT.id,
  expires_at: "2026-10-08T10:10:00Z",
};

function mount(answer: UpdateState, onBack = vi.fn()) {
  get.mockImplementation(async (path: string) => {
    if (path === "/admin/application/update") return answer;
    throw new Error(`unexpected GET ${path}`);
  });
  post.mockImplementation(async (path: string) => {
    if (path === "/admin/application/upstream") return UPSTREAM;
    if (path === "/admin/application/update/recovery-code") return CODE;
    if (path === "/me/step-up") return { token: "grant-1" };
    if (path === "/admin/application/update/apply")
      return { id: "req-1", kind: "apply", to_version: "0.10.0" };
    if (path.startsWith("/admin/application/update/")) return { id: "req-2", kind: "x", to_version: null };
    throw new Error(`unexpected POST ${path}`);
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Updates repository="https://github.com/Example/spend-tracker" commit="987afef00000" onBack={onBack} />
    </QueryClientProvider>,
  );
  return onBack;
}

async function checkNow() {
  fireEvent.click(await screen.findByRole("button", { name: "Check the repository" }));
  await screen.findByText("0.10.0 is available.");
}

// --------------------------------------------------------------------------- //
// A5: the five cases
// --------------------------------------------------------------------------- //

describe("A5: what the section says in each case", () => {
  it("a checkout: the terminal commands, under a newer version, and no Prepare", async () => {
    mount(state({ case: "not_container", heartbeat: null }));
    await checkNow();
    const said = document.querySelector('[data-case="not_container"]')!;
    expect(said.textContent).toContain("This instance runs from a checkout.");
    expect(said.textContent).toContain("make upgrade-check, then make upgrade");
    expect(screen.queryByRole("button", { name: /^Prepare/ })).toBeNull();
  });

  it("a container with no heartbeat: names the service, since no container is known", async () => {
    mount(state({ case: "no_updater", heartbeat: null }));
    await checkNow();
    const said = document.querySelector('[data-case="no_updater"]')!;
    expect(said.textContent).toContain("Updating from this screen needs the updater");
    expect(said.textContent).toContain("find the updater service of the spend-tracker project and press Start");
    expect(screen.queryByRole("button", { name: /^Prepare/ })).toBeNull();
  });

  it("a container with a stale heartbeat: names the container the heartbeat named", async () => {
    mount(
      state({
        case: "no_updater",
        heartbeat: beat({ fresh: false, container: "spend-tracker_updater_1", engine: "podman" }),
      }),
    );
    await checkNow();
    expect(document.querySelector('[data-case="no_updater"]')!.textContent).toContain(
      "find spend-tracker_updater_1 and press Start",
    );
  });

  it("refused: the updater's own sentence, and no Prepare", async () => {
    const why =
      "Docker Desktop's Enhanced Container Isolation does not let containers use the Docker socket.";
    mount(state({ case: "refused", heartbeat: beat({ socket: "eci_blocked", socket_sentence: why }) }));
    const said = await screen.findByText((_, node) => node?.getAttribute("data-case") === "refused");
    expect(said.textContent).toContain(why);
    expect(said.textContent).toContain("replacing the compose bundle");
    await checkNow();
    expect(screen.queryByRole("button", { name: /^Prepare/ })).toBeNull();
  });

  it("outdated: names the engine and the updater, and Update the updater asks for the newest one", async () => {
    mount(
      state({
        case: "outdated",
        heartbeat: beat({ socket: "outdated", engine: "podman-machine", engine_version: "5.6.1" }),
      }),
    );
    const said = await screen.findByText((_, node) => node?.getAttribute("data-case") === "outdated");
    expect(said.textContent).toContain("Podman 5.6.1 (podman machine) is newer than this updater (0.8.0)");
    expect(said.textContent).toContain("download the newest Spend Tracker zip");

    fireEvent.click(within(said as HTMLElement).getByRole("button", { name: "Update the updater" }));
    await vi.waitFor(() =>
      expect(post).toHaveBeenCalledWith("/admin/application/update/updater", { to_version: "0.10.0" }),
    );
    // It checked first, because nobody had: one request to the repository.
    expect(post.mock.calls.filter(([path]) => path === "/admin/application/upstream")).toHaveLength(1);
  });

  it("working: the updater's container, version and engine; then Prepare the newest", async () => {
    mount(state());
    const said = await screen.findByText((_, node) => node?.getAttribute("data-case") === "working");
    expect(said.textContent).toBe(
      "Updates run in the updater (spend-tracker-updater-1, version 0.8.0, on Docker Desktop 4.48.0). Nothing installs until you confirm it.",
    );
    await checkNow();
    expect(screen.getByText(/Installing it also installs 0\.9\.0, which it skips over/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Prepare 0.10.0" }));
    await vi.waitFor(() =>
      expect(post).toHaveBeenCalledWith("/admin/application/update/prepare", { to_version: "0.10.0" }),
    );
  });

  it("working: another newer release can be chosen, and only its notes show", async () => {
    mount(state());
    await checkNow();
    expect(document.querySelectorAll(".release-notes")).toHaveLength(2);
    fireEvent.change(screen.getByLabelText("Another newer release"), { target: { value: "0.9.0" } });
    expect(screen.getByText("0.9.0 is available.")).toBeTruthy();
    expect([...document.querySelectorAll(".release-notes")].map((n) => n.getAttribute("data-version"))).toEqual([
      "0.9.0",
    ]);
    fireEvent.click(screen.getByRole("button", { name: "Prepare 0.9.0" }));
    await vi.waitFor(() =>
      expect(post).toHaveBeenCalledWith("/admin/application/update/prepare", { to_version: "0.9.0" }),
    );
  });

  it("working: a newer updater is offered on its own (C2)", async () => {
    mount(state());
    await checkNow();
    fireEvent.click(screen.getByRole("button", { name: "Update the updater only" }));
    await vi.waitFor(() =>
      expect(post).toHaveBeenCalledWith("/admin/application/update/updater", { to_version: "0.10.0" }),
    );
  });
});

// --------------------------------------------------------------------------- //
// A7: notes are text
// --------------------------------------------------------------------------- //

describe("A7: release notes render as text", () => {
  it("markup in a release body arrives as characters, not elements", () => {
    const notes = '<img src=x onerror="alert(1)"><b>bold</b>\n<script>alert(2)</script>';
    render(
      <ReleaseNotes
        releases={[{ ...UPSTREAM.releases[0], notes }, { ...UPSTREAM.releases[1], notes: "plain\nlines" }]}
      />,
    );
    const first = document.querySelector('.release-notes[data-version="0.10.0"]')!;
    expect(first.textContent).toBe(notes);
    expect(first.querySelector("img, b, script")).toBeNull();
    expect(document.querySelector('.release-notes[data-version="0.9.0"]')!.textContent).toBe("plain\nlines");
  });
});

// --------------------------------------------------------------------------- //
// A8: the confirmation
// --------------------------------------------------------------------------- //

describe("A8: the Update button needs every box", () => {
  function fill() {
    fireEvent.change(screen.getByLabelText("Your password"), { target: { value: "a password" } });
    fireEvent.change(screen.getByLabelText("The six digits from your authenticator"), {
      target: { value: "123456" },
    });
  }

  it("two lossy migrations, the recovery code and both factors, then the exact apply", async () => {
    mount(state({ report: REPORT }));
    const button = await screen.findByRole("button", { name: "Update to 0.10.0" });
    // The code was asked for with a POST, when the confirmation was drawn.
    expect(await screen.findByTestId("recovery-code")).toHaveProperty("textContent", CODE.code);
    expect(post).toHaveBeenCalledWith("/admin/application/update/recovery-code");
    expect(get).not.toHaveBeenCalledWith("/admin/application/update/recovery-code");

    const boxes = screen.getAllByRole("checkbox", { name: /cannot be undone except from the backup/ });
    expect(boxes).toHaveLength(2);
    const saved = screen.getByRole("checkbox", { name: "I have saved the recovery code." });

    fill();
    expect(button).toHaveProperty("disabled", true);
    fireEvent.click(boxes[0]);
    expect(button).toHaveProperty("disabled", true);
    fireEvent.click(boxes[1]);
    expect(button).toHaveProperty("disabled", true); // still not the code's box
    fireEvent.click(saved);
    expect(button).toHaveProperty("disabled", false);
    fireEvent.click(boxes[0]);
    expect(button).toHaveProperty("disabled", true); // one lossy box short
    fireEvent.click(boxes[0]);
    fireEvent.change(screen.getByLabelText("Your password"), { target: { value: "" } });
    expect(button).toHaveProperty("disabled", true); // no password
    fill();
    expect(button).toHaveProperty("disabled", false);

    fireEvent.click(button);
    await vi.waitFor(() =>
      expect(post).toHaveBeenCalledWith("/admin/application/update/apply", {
        prepared_id: REPORT.id,
        digest: DIGEST,
        updater_digest: UPDATER_DIGEST,
        accepted_lossy: ["b2c3d4e5f6a1", "d4e5f6a1b2c3"],
        recovery_code_id: "code-1",
        step_up_token: "grant-1",
      }),
    );
    expect(post).toHaveBeenCalledWith("/me/step-up", { password: "a password", code: "123456" });
    // The full-width panel takes over.
    expect(await screen.findByRole("dialog", { name: "Updating to 0.10.0" })).toBeTruthy();
  });

  it("lists every migration with its verdict, and says what the backup is for", async () => {
    mount(state({ report: REPORT }));
    await screen.findByText("3 migrations will run");
    const items = [...document.querySelectorAll(".update-migrations li")].map((li) => li.textContent);
    expect(items[0]).toContain("b2c3d4e5f6a1 Fold theme colours — rolling back: lossy — old values go");
    expect(items[1]).toContain("c3d4e5f6a1b2 Index receipts — rolling back: clean");
    expect(items[2]).toContain("rolling back: undeclared");
    expect(screen.getByText("2 of these cannot be undone by a downgrade")).toBeTruthy();
    // On Docker Desktop, the laptop row shows; the commit comes from the attestation.
    expect(screen.getByText("On a laptop")).toBeTruthy();
    expect(screen.getByText("(9c41e0b)")).toBeTruthy();
  });

  it("Discard sends the report's id", async () => {
    mount(state({ report: REPORT }));
    fireEvent.click(await screen.findByRole("button", { name: "Discard" }));
    await vi.waitFor(() =>
      expect(post).toHaveBeenCalledWith("/admin/application/update/discard", { prepared_id: REPORT.id }),
    );
  });
});

// --------------------------------------------------------------------------- //
// 3.3: preparing
// --------------------------------------------------------------------------- //

describe("preparing", () => {
  it("shows the updater's sentences, the last one still running", async () => {
    mount(
      state({
        in_flight: true,
        status: {
          state: "running",
          id: "p-1",
          kind: "prepare",
          step: "P4",
          sentences: ["found the release image", "checked its origin"],
          updated_at: null,
        },
      }),
    );
    expect(await screen.findByText("Preparing the update")).toBeTruthy();
    const steps = [...document.querySelectorAll(".update-steps li")].map((li) => li.textContent);
    expect(steps).toEqual(["✓ found the release image", "… checked its origin"]);
    expect(screen.queryByRole("button", { name: "Check the repository" })).toBeNull();
  });
});

// --------------------------------------------------------------------------- //
// A9: every outcome renders
// --------------------------------------------------------------------------- //

describe("A9: each outcome", () => {
  const base: Outcome = {
    id: "0d3c7c5e-2222-4b2b-9c3d-000000000002",
    kind: "apply",
    state: "succeeded",
    sentence: "The update finished.",
    code: null,
    finished_at: "2026-10-08T21:14:00Z",
    started_at: "2026-10-08T21:11:20Z",
    failed_step: null,
    backup: "20261008-211120",
    duration_s: 160,
    gap_s: 0,
    log_tail: [],
  };

  function show(outcome: Outcome, heartbeat: Heartbeat | null = beat({ updater_version: "0.10.0" })) {
    post.mockResolvedValue(null);
    const onChanged = vi.fn();
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <OutcomeBlock
          outcome={outcome}
          running={outcome.state === "succeeded" ? "0.10.0" : "0.8.0"}
          heartbeat={heartbeat}
          lastPrepared="0.10.0"
          onPrepare={vi.fn()}
          onChanged={onChanged}
        />
      </QueryClientProvider>,
    );
    return onChanged;
  }

  it("updated, with the updater updated too, and Dismiss marks it seen", async () => {
    const onChanged = show(base);
    const said = document.querySelector('[data-outcome="updated"]')!;
    expect(said.textContent).toMatch(/^Updated to 0\.10\.0 at .+ in 2 min 40 s\. The updater is now 0\.10\.0 too\.$/);
    expect(screen.getByText(/listed under Database/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(post).toHaveBeenCalledWith(`/admin/application/update/outcome/${base.id}/seen`);
  });

  it("updated, but the updater stayed behind: Retry updater update", async () => {
    show(base, beat({ updater_version: "0.8.0" }));
    expect(screen.getByText(/The updater stayed on 0\.8\.0 and will try again/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry updater update" }));
    await vi.waitFor(() =>
      expect(post).toHaveBeenCalledWith("/admin/application/update/updater", { to_version: "0.10.0" }),
    );
  });

  it("rolled back: the step, the updater's sentence and the log's end", () => {
    show({
      ...base,
      state: "rolled_back",
      failed_step: "migrate",
      sentence: "alembic upgrade failed on b2c3d4e5f6a1.",
      log_tail: ["INFO running b2c3d4e5f6a1", "ERROR no such column"],
    });
    const said = document.querySelector('[data-outcome="rolled_back"]')!;
    expect(said.textContent).toContain("The update failed at migrate, so it was undone. You are on 0.8.0");
    expect(said.textContent).toContain("alembic upgrade failed on b2c3d4e5f6a1.");
    expect(said.querySelector("pre")!.textContent).toBe("INFO running b2c3d4e5f6a1\nERROR no such column");
    expect(said.textContent).toContain("The image is kept so you can try again.");
  });

  it("not started, and refused, say nothing was changed", () => {
    show({ ...base, state: "not_started", sentence: "There is not enough disk space." });
    expect(document.querySelector('[data-outcome="not_started"]')!.textContent).toBe(
      "The update did not start: There is not enough disk space. Nothing was changed.",
    );
    cleanup();
    show({ ...base, state: "refused", sentence: "The digests do not match the report." });
    expect(document.querySelector('[data-outcome="not_started"]')!.textContent).toContain(
      "The digests do not match the report. Nothing was changed.",
    );
  });

  it("recovered: what was done, from which backup, and the version now running", () => {
    show({ ...base, state: "recovered", sentence: "The backup was restored by hand." });
    const said = document.querySelector('[data-outcome="recovered"]')!;
    expect(said.textContent).toContain("The recovery page was used for the last update.");
    expect(said.textContent).toContain("The backup was restored by hand.");
    expect(said.textContent).toContain("Restored from 20261008-211120.");
    expect(said.textContent).toContain("This instance now runs 0.8.0.");
  });

  it("left for the operator", () => {
    show({ ...base, state: "left_for_operator", sentence: "Stopped at the owner's request." });
    expect(document.querySelector('[data-outcome="left_for_operator"]')!.textContent).toContain(
      "left to be finished from a terminal",
    );
  });

  it("a failed prepare offers Try again with the same version", () => {
    const onPrepare = vi.fn();
    post.mockResolvedValue(null);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <OutcomeBlock
          outcome={{ ...base, kind: "prepare", state: "refused", sentence: "The attestation did not verify." }}
          running="0.8.0"
          heartbeat={beat()}
          lastPrepared="0.10.0"
          onPrepare={onPrepare}
          onChanged={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(document.querySelector('[data-outcome="prepare_failed"]')!.textContent).toContain(
      "Preparing the update failed: The attestation did not verify. Nothing was changed.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onPrepare).toHaveBeenCalledWith("0.10.0");
  });
});

// --------------------------------------------------------------------------- //
// 3.5: updating, and the 30-minute fallback
// --------------------------------------------------------------------------- //

describe("updating", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("polls health every 3 s once the app is gone, says 'not back yet' at 30 min, and reloads when back", async () => {
    let healthy = false;
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/health") {
        if (!healthy) throw new TypeError("Failed to fetch");
        return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
      }
      // The app has already stopped: its state answers a maintenance page.
      return new Response("<html>being updated</html>", { status: 503 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const onBack = vi.fn();
    render(<Updating toVersion="0.10.0" initial={["backed up the ledger"]} onBack={onBack} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(screen.getByText("The app has stopped. Waiting for it to come back…")).toBeTruthy();
    // The last progress it saw is kept.
    expect(screen.getByText(/backed up the ledger/)).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOT_BACK_AFTER_MS - 10_000);
    });
    const healthCalls = fetchMock.mock.calls.filter(([url]) => url === "/api/health").length;
    expect(healthCalls).toBeGreaterThan((NOT_BACK_AFTER_MS - 20_000) / HEALTH_POLL_MS);
    expect(screen.queryByText(/The app has not come back yet/)).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(screen.getByText(/The app has not come back yet/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open the recovery page" }).getAttribute("href")).toBe("/recovery");
    expect(onBack).not.toHaveBeenCalled();

    healthy = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(HEALTH_POLL_MS);
    });
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("once the app is back, opens Application management rather than reloading onto the register", async () => {
    // A bare reload landed on the register, because the shell holds its
    // screen in memory only, and the owner never saw the outcome.
    const assign = vi.fn();
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign, reload });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url === "/api/admin/application/update")
          return new Response(JSON.stringify(state({ in_flight: false })), { status: 200 });
        throw new TypeError(`unexpected fetch ${url}`);
      }),
    );
    get.mockImplementation(async () =>
      state({
        in_flight: true,
        status: { state: "running", id: "a", kind: "apply", step: "2", sentences: [], updated_at: null },
      }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Updates repository="https://github.com/Example/spend-tracker" commit="987afef00000" />
      </QueryClientProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByRole("dialog", { name: "Updating" })).toBeTruthy();
    expect(assign).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(APPLY_POLL_MS);
    });
    expect(assign.mock.calls).toEqual([["/?open=application#updates"]]);
    expect(reload).not.toHaveBeenCalled();
  });

  it("while the app still answers, it shows the updater's sentences", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify(
            state({
              in_flight: true,
              status: {
                state: "running",
                id: "a",
                kind: "apply",
                step: "2",
                sentences: ["preflight passed", "handing over to the new updater"],
                updated_at: null,
              },
            }),
          ),
          { status: 200 },
        ),
      ),
    );
    render(<Updating toVersion="0.10.0" initial={[]} onBack={vi.fn()} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(screen.getByText(/handing over to the new updater/)).toBeTruthy();
    expect(screen.queryByText(/The app has stopped/)).toBeNull();
  });
});

// --------------------------------------------------------------------------- //
// 8.7: update backups
// --------------------------------------------------------------------------- //

describe("update backups", () => {
  function folder(stamp: string, version: string, protectedOne: boolean): Backup {
    return {
      name: stamp,
      path: `/data/backups/${stamp}`,
      bytes: 4096,
      made_at: `${stamp.slice(0, 4)}-${stamp.slice(4, 6)}-${stamp.slice(6, 8)}T10:00:00Z`,
      kind: "update",
      version,
      revision: "2de003489b79",
      protected: protectedOne,
    };
  }

  it("offers Delete only on the ones older than the newest five, and sorts by version as numbers", async () => {
    del.mockResolvedValue(null);
    const rows = [
      folder("20261001-100000", "0.9.0", true),
      folder("20260901-100000", "0.10.0", false),
      folder("20260801-100000", "0.2.0", false),
    ];
    render(
      <QueryClientProvider client={new QueryClient()}>
        <UpdateBackupList backups={rows} onChanged={vi.fn()} />
      </QueryClientProvider>,
    );
    const body = () => screen.getAllByRole("row").slice(1);
    expect(body().map((row) => within(row).getAllByRole("cell")[0].textContent)).toEqual([
      "20261001-100000",
      "20260901-100000",
      "20260801-100000",
    ]);
    expect(within(body()[0]).queryByRole("button", { name: "Delete" })).toBeNull();
    expect(within(body()[0]).getByText("kept")).toBeTruthy();
    expect(within(body()[1]).getByRole("button", { name: "Delete" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Version/ }));
    // As numbers: as text, 0.10.0 would come before 0.2.0.
    expect(body().map((row) => within(row).getAllByRole("cell")[1].textContent)).toEqual([
      "0.2.0",
      "0.9.0",
      "0.10.0",
    ]);

    fireEvent.click(within(body()[0]).getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog", { name: "Delete this update backup?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Yes, delete it" }));
    await vi.waitFor(() =>
      expect(del).toHaveBeenCalledWith("/admin/application/backups/20260801-100000"),
    );
  });
});
