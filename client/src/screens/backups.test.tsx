// @vitest-environment jsdom

/**
 * The backups list and its save panel (#133).
 *
 * What is pinned: no link ever carries the key -- once the box is ticked the
 * download goes through the panel, which buys a step-up grant with the
 * password and a code and spends it on a POST (#204);
 * the delete confirmation names the file and says so in words when it is the
 * only or the newest backup, and confirming sends the delete for *that* file;
 * the list sorts at its headers; and the save panel offers only the routes the
 * browser really has, with the plain download always there.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { availableRoutes, backupDownloadUrl } from "../lib/saveBackup";
import { BackupList, SavePanel, type Backup } from "./Backups";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const OLDER: Backup = {
  name: "spendtracker-20260901T080000Z.sqlite3",
  path: "/data/backups/spendtracker-20260901T080000Z.sqlite3",
  bytes: 2048,
  made_at: "2026-09-01T08:00:00Z",
};
const NEWER: Backup = {
  name: "spendtracker-20260920T080000Z.sqlite3",
  path: "/data/backups/spendtracker-20260920T080000Z.sqlite3",
  bytes: 1024,
  made_at: "2026-09-20T08:00:00Z",
};

function mount(backups: Backup[], onChanged = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BackupList backups={backups} onChanged={onChanged} />
    </QueryClientProvider>,
  );
  return onChanged;
}

function names(): string[] {
  return screen
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0].textContent ?? "");
}

describe("the list", () => {
  it("opens newest first and sorts by name at its heading", () => {
    mount([OLDER, NEWER]);
    expect(names()).toEqual([NEWER.name, OLDER.name]);

    fireEvent.click(screen.getByRole("button", { name: /Backup/ }));
    expect(names()).toEqual([OLDER.name, NEWER.name]);

    fireEvent.click(screen.getByRole("button", { name: /Size/ }));
    expect(names()).toEqual([NEWER.name, OLDER.name]); // 1 KiB before 2 KiB
  });

  it("never puts the key in a link: once ticked, Download asks for both factors", () => {
    mount([OLDER, NEWER]);
    const links = () => screen.queryAllByRole("link", { name: "Download" });
    expect(links()).toHaveLength(2);
    for (const link of links()) expect(link.getAttribute("href")).not.toContain("include_key");
    expect(links()[0].getAttribute("download")).toBe("spendtracker-20260920T080000Z.zip");

    fireEvent.click(screen.getByRole("checkbox", { name: /secret\.key/ }));
    expect(links()).toHaveLength(0);
    expect(screen.getByText(/decrypts every authenticator/)).toBeTruthy();

    fireEvent.click(screen.getAllByRole("button", { name: "Download…" })[0]);
    const dialog = screen.getByRole("dialog", { name: "Save to Google Drive or Dropbox" });
    expect(within(dialog).getByText("with secret.key")).toBeTruthy();
    expect(within(dialog).getByLabelText("Your password")).toBeTruthy();
  });

  it("says nothing is backed up when nothing is", () => {
    mount([]);
    expect(screen.getByText("Nothing backed up yet.")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("deleting", () => {
  it("warns in words when it is the only backup, and deletes that one", async () => {
    vi.mocked(api.del).mockResolvedValue(null);
    const changed = mount([OLDER]);

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog", { name: "Delete this backup?" });
    expect(within(dialog).getByText(OLDER.name)).toBeTruthy();
    expect(within(dialog).getByText("This is the only backup.")).toBeTruthy();

    fireEvent.click(within(dialog).getByRole("button", { name: "Yes, delete it" }));
    await vi.waitFor(() => expect(changed).toHaveBeenCalled());
    expect(api.del).toHaveBeenCalledWith(`/admin/application/backups/${OLDER.name}`);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("warns about the newest, not about an older one, and Keep deletes nothing", () => {
    mount([OLDER, NEWER]);
    const deletes = () => screen.getAllByRole("button", { name: "Delete" });

    fireEvent.click(deletes()[0]); // newest first
    expect(screen.getByText("This is the newest backup.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));

    fireEvent.click(deletes()[1]);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(OLDER.name)).toBeTruthy();
    expect(within(dialog).queryByText(/newest backup|only backup/)).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep it" }));

    expect(api.del).not.toHaveBeenCalled();
  });
});

describe("saving to Drive or Dropbox", () => {
  function panel(routes: ("share" | "folder" | "download")[], withKey = false) {
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <SavePanel backup={NEWER} withKey={withKey} onClose={() => {}} routes={routes} />
      </QueryClientProvider>,
    );
    return screen.getByRole("dialog", { name: "Save to Google Drive or Dropbox" });
  }

  it("offers only the download where nothing else exists, and it is step one", () => {
    const dialog = panel(["download"]);
    expect(within(dialog).queryByText(/Share it/)).toBeNull();
    expect(within(dialog).queryByText(/synced Drive or Dropbox folder/)).toBeNull();
    expect(within(dialog).getByText("1. Download it, then upload it")).toBeTruthy();
    const drive = within(dialog).getByRole("link", { name: "Open Google Drive" });
    expect(drive.getAttribute("target")).toBe("_blank");
    expect(drive.getAttribute("rel")).toContain("noopener");
  });

  it("lists every route the browser has, best first, with the key if asked", () => {
    const dialog = panel(["share", "folder", "download"], true);
    const headings = within(dialog)
      .getAllByRole("heading", { level: 3 })
      .map((one) => one.textContent);
    expect(headings).toEqual([
      "1. Share it to the Drive or Dropbox app",
      "2. Save it into your synced Drive or Dropbox folder",
      "3. Or download it and upload it",
    ]);
    expect(within(dialog).getByText("with secret.key")).toBeTruthy();
    // With the key there is no link to follow, and nothing to press until
    // both factors are in.
    expect(within(dialog).queryByRole("link", { name: /^Download / })).toBeNull();
    const download = within(dialog).getByRole("button", {
      name: "Download spendtracker-20260920T080000Z.zip",
    });
    expect((download as HTMLButtonElement).disabled).toBe(true);
    expect(
      (within(dialog).getByRole("button", { name: "Prepare the zip" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("without the key the download is the plain link", () => {
    const dialog = panel(["download"], false);
    expect(within(dialog).queryByLabelText("Your password")).toBeNull();
    expect(
      within(dialog)
        .getByRole("link", { name: "Download spendtracker-20260920T080000Z.zip" })
        .getAttribute("href"),
    ).toBe(backupDownloadUrl(NEWER.name));
  });

  it("with the key, buys a grant and spends it on a POST, then empties the code", async () => {
    vi.mocked(api.post).mockResolvedValue({ token: "grant-once" });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      blob: async () => new Blob(["zip"], { type: "application/zip" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const created = vi.fn(() => "blob:zip");
    const revoked = vi.fn();
    const original = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    URL.createObjectURL = created;
    URL.revokeObjectURL = revoked;
    try {
      const dialog = panel(["download"], true);
      fireEvent.change(within(dialog).getByLabelText("Your password"), {
        target: { value: "a sufficiently long password" },
      });
      const code = within(dialog).getByLabelText(
        "The six digits from your authenticator",
      ) as HTMLInputElement;
      fireEvent.change(code, { target: { value: "123456" } });

      fireEvent.click(
        within(dialog).getByRole("button", { name: "Download spendtracker-20260920T080000Z.zip" }),
      );
      await vi.waitFor(() => expect(created).toHaveBeenCalled());

      expect(api.post).toHaveBeenCalledWith("/me/step-up", {
        password: "a sufficiently long password",
        code: "123456",
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0];
      expect(url).toBe(backupDownloadUrl(NEWER.name));
      expect(init.method).toBe("POST");
      expect(JSON.parse(init.body)).toEqual({ include_key: true, step_up_token: "grant-once" });
      // The code bought the grant and is dead; the field says so.
      expect(code.value).toBe("");
    } finally {
      URL.createObjectURL = original.create;
      URL.revokeObjectURL = original.revoke;
      vi.unstubAllGlobals();
    }
  });
});

describe("which routes a browser has", () => {
  const share = vi.fn();

  it("is only the download over plain http, whatever else exists", () => {
    expect(
      availableRoutes({
        isSecureContext: false,
        navigator: { share, canShare: () => true },
        showSaveFilePicker: () => {},
      }),
    ).toEqual(["download"]);
  });

  it("shares only when the browser says it can share a zip", () => {
    expect(
      availableRoutes({ isSecureContext: true, navigator: { share, canShare: () => true } }),
    ).toEqual(["share", "download"]);
    expect(
      availableRoutes({ isSecureContext: true, navigator: { share, canShare: () => false } }),
    ).toEqual(["download"]);
  });

  it("saves into a folder where the picker exists", () => {
    expect(
      availableRoutes({ isSecureContext: true, navigator: {}, showSaveFilePicker: () => {} }),
    ).toEqual(["folder", "download"]);
  });

  it("encodes the name into the download URL", () => {
    expect(backupDownloadUrl("a b.sqlite3")).toBe(
      "/api/admin/application/backups/a%20b.sqlite3/download",
    );
  });
});
