// @vitest-environment jsdom

/**
 * Going back one release from the Updates section (#173).
 *
 * Pinned: the offer shows only when the server makes one; the confirmation
 * says what would be lost, per household -- two of them, and the instance's
 * own changes -- and the passkey caveat; the button needs the version typed,
 * the recovery code saved and both factors; the request names exactly the
 * update and the code it was shown; an updater too old to go back is said and
 * the button stays off; and the outcome of a go-back renders.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { OutcomeBlock, Updates, type GoBackLoss, type GoBackOffer, type Heartbeat, type UpdateState } from "./Updates";

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function beat(over: Partial<Heartbeat> = {}): Heartbeat {
  return {
    seen_at: "2026-11-08T10:00:00Z",
    fresh: true,
    updater_version: "0.13.0",
    image_digest: "sha256:" + "1111".repeat(16),
    engine: "docker-engine",
    engine_version: "29.8.2",
    rootless: false,
    layout: "loopback",
    socket: "ok",
    hook: false,
    busy: false,
    role: "current",
    protocols: "1-2",
    api_version: "1.47",
    engine_api: "1.40-1.56",
    container: "spend-tracker-updater-1",
    socket_sentence: null,
    podman_restart: "not_applicable",
    problem: null,
    ...over,
  } as Heartbeat;
}

const OFFER: GoBackOffer = {
  update_id: "4f1c2e8a-6b3d-4c5e-9f70-1a2b3c4d5e6f",
  to_version: "0.12.0",
  from_version: "0.13.0",
  updated_at: "2026-11-08T09:00:00Z",
  backup: "20261108-085900",
  updater_can: true,
};

const LOSS: GoBackLoss = {
  offer: OFFER,
  since: "2026-11-08T08:59:00Z",
  households: [
    { household_id: "h2", name: "Allotment", batches: 1 },
    { household_id: "h1", name: "Second home", batches: 4 },
    { household_id: null, name: null, batches: 2 },
  ],
  passkeys: 2,
};

const CODE = { id: "code-9", code: "KQ7M-AB12-CD34-EF56-GH78-JK9M-W2PX", prepared_id: OFFER.update_id, expires_at: "2026-11-08T10:10:00Z" };

function state(over: Partial<UpdateState> = {}): UpdateState {
  return {
    case: "working",
    running: "0.13.0",
    protocol: 2,
    in_flight: false,
    heartbeat: beat(),
    status: null,
    report: null,
    outcome: null,
    backups: [],
    go_back: OFFER,
    ...over,
  };
}

function mount(answer: UpdateState, loss: GoBackLoss = LOSS) {
  get.mockImplementation(async (path: string) => {
    if (path === "/admin/application/update") return answer;
    if (path === "/admin/application/update/go-back") return loss;
    throw new Error(`unexpected GET ${path}`);
  });
  post.mockImplementation(async (path: string) => {
    if (path === "/admin/application/update/go-back/recovery-code") return CODE;
    if (path === "/me/step-up") return { token: "grant-9" };
    if (path === "/admin/application/update/go-back") return { id: "req-9", kind: "go_back", to_version: "0.12.0" };
    throw new Error(`unexpected POST ${path}`);
  });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <Updates repository="https://github.com/Example/spend-tracker" commit="987afef00000" onBack={vi.fn()} />
    </QueryClientProvider>,
  );
}

async function openConfirm() {
  fireEvent.click(await screen.findByRole("button", { name: "Go back to 0.12.0…" }));
  return screen.findByRole("button", { name: "Go back to 0.12.0" });
}

describe("going back (#173)", () => {
  it("no offer, no section", async () => {
    mount(state({ go_back: null }));
    await screen.findByRole("button", { name: "Check the repository" });
    expect(document.querySelector("[data-go-back]")).toBeNull();
  });

  it("says what is lost per household and the passkey caveat", async () => {
    mount(state());
    await openConfirm();
    const lost = await screen.findByRole("list");
    expect([...lost.querySelectorAll("li")].map((li) => li.textContent)).toEqual([
      "Allotment: 1 change",
      "Second home: 4 changes",
      "2 changes to the instance itself (people, settings)",
    ]);
    const passkeys = document.querySelector("[data-passkeys]")!;
    expect(passkeys.getAttribute("data-passkeys")).toBe("2");
    expect(passkeys.textContent).toMatch(/^2 passkeys were registered since then\. A passkey registered after the update stops working/);
    expect(screen.getByText(/The backup/).textContent).toContain("20261108-085900");
  });

  it("nothing written since says so", async () => {
    mount(state(), { ...LOSS, households: [], passkeys: 0 });
    await openConfirm();
    expect((await screen.findByText("Nothing has been written to the ledger since then.")).getAttribute("data-lost")).toBe(
      "nothing",
    );
    expect(document.querySelector("[data-passkeys]")!.textContent).toMatch(/^A passkey registered after the update/);
  });

  it("needs the version typed, the code saved and both factors, then sends exactly that update", async () => {
    mount(state());
    const button = await openConfirm();
    expect(await screen.findByTestId("go-back-recovery-code")).toHaveProperty("textContent", CODE.code);
    await screen.findByRole("list");
    const fill = () => {
      fireEvent.change(screen.getByLabelText("Your password"), { target: { value: "a password" } });
      fireEvent.change(screen.getByLabelText("The six digits from your authenticator"), { target: { value: "123456" } });
    };
    fill();
    fireEvent.click(screen.getByRole("checkbox", { name: "I have saved the recovery code." }));
    expect(button).toHaveProperty("disabled", true); // nothing typed
    fireEvent.change(screen.getByTestId("go-back-typed"), { target: { value: "0.13.0" } });
    expect(button).toHaveProperty("disabled", true); // the running version, not the one to go back to
    fireEvent.change(screen.getByTestId("go-back-typed"), { target: { value: "0.12.0" } });
    expect(button).toHaveProperty("disabled", false);
    fireEvent.click(button);
    await vi.waitFor(() =>
      expect(post).toHaveBeenCalledWith("/admin/application/update/go-back", {
        update_id: OFFER.update_id,
        typed_version: "0.12.0",
        recovery_code_id: "code-9",
        step_up_token: "grant-9",
      }),
    );
    expect(post).toHaveBeenCalledWith("/me/step-up", { password: "a password", code: "123456" });
    // The full-width panel takes over, as for an update.
    expect(await screen.findByRole("dialog")).toBeTruthy();
  });

  it("an updater too old to go back is said, and the button stays off", async () => {
    mount(state({ go_back: { ...OFFER, updater_can: false } }), { ...LOSS, offer: { ...OFFER, updater_can: false } });
    const button = await openConfirm();
    expect(document.querySelector('[data-go-back-updater="too-old"]')).toBeTruthy();
    fireEvent.change(screen.getByTestId("go-back-typed"), { target: { value: "0.12.0" } });
    expect(button).toHaveProperty("disabled", true);
  });

  it("the outcome of a go-back, and of one that did not start", () => {
    const base = {
      id: "4f1c2e8a-6b3d-4c5e-9f70-000000000009",
      kind: "go_back",
      state: "succeeded",
      sentence: "Went back to 0.12.0.",
      code: null,
      finished_at: "2026-11-08T10:05:00Z",
      started_at: "2026-11-08T10:03:00Z",
      failed_step: null,
      backup: null,
      duration_s: 90,
      gap_s: 0,
      log_tail: [],
      to_version: "0.12.0",
    };
    const show = (outcome: typeof base) =>
      render(
        <QueryClientProvider client={new QueryClient()}>
          <OutcomeBlock outcome={outcome} running="0.12.0" heartbeat={beat()} lastPrepared={null} onPrepare={vi.fn()} onChanged={vi.fn()} />
        </QueryClientProvider>,
      );
    show(base);
    expect(document.querySelector('[data-outcome="went_back"]')!.textContent).toMatch(/^Went back to 0\.12\.0 at .+\. The ledger is as it was before the update/);
    cleanup();
    show({ ...base, state: "not_started", sentence: "Not started: The image of 0.12.0 is no longer on this machine." });
    expect(document.querySelector('[data-outcome="go_back_not_started"]')!.textContent).toBe(
      "Going back did not start: Not started: The image of 0.12.0 is no longer on this machine. Nothing was changed.",
    );
  });
});
