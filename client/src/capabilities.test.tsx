// @vitest-environment jsdom

/**
 * What the client hides when the deployment does not offer it (#316).
 *
 * The server has already left these routes out, so what is pinned is that the
 * screens no longer lead to them: the menu entry gone, the Admin tab and
 * buttons gone, and -- the part a user would not see -- no request made to a
 * route that answers 404. With everything on, which is every server, each is
 * exactly where it was.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("./lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
  setUnauthorizedHandler: vi.fn(),
}));

import { api } from "./lib/api";
import { EVERYTHING, loadCapabilities, setCapabilities, useCapabilities, type Capabilities } from "./lib/capabilities";
import type { AdminUser, User } from "./lib/types";
import { menu } from "./App";
import { Admin } from "./screens/Admin";

const DESKTOP: Capabilities = {
  ...EVERYTHING,
  snap: false,
  agents: false,
  invitations: false,
  account_resets: false,
  passkeys: false,
  second_factor: false,
  password_reset_by_code: true,
  self_update: false,
  db_view: false,
};

function person(id: string, name: string, role: "owner" | "member"): AdminUser {
  return {
    id,
    email: `${id}@example.test`,
    display_name: name,
    role,
    created_at: "2026-09-01T00:00:00Z",
    disabled_at: null,
    recovery_codes_left: 10,
    households: [],
  };
}

const USERS = [
  person("u-jane", "Jane", "owner"),
  person("u-alex", "Alex", "owner"),
  person("u-sam", "Sam", "member"),
  person("u-robin", "Robin", "member"),
];
const ME = { id: "u-jane", email: "u-jane@example.test", display_name: "Jane", role: "owner" } as User;

function answer(offered: Capabilities) {
  // As the boot leaves it: asked once, before any screen is drawn.
  setCapabilities(offered);
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path === "/capabilities") return offered;
    if (path === "/admin/users") return USERS;
    if (path === "/admin/households") return [];
    if (path === "/admin/resets") return [];
    if (path.startsWith("/admin/sign-in-changes")) return [];
    return [];
  });
}

function renderAdmin() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Admin user={ME} />
    </QueryClientProvider>,
  );
}

/** The tab row's buttons, by name, in order. */
const tabs = () =>
  within(screen.getByRole("button", { name: "People" }).parentElement!)
    .getAllByRole("button")
    .map((tab) => tab.textContent);

const asked = () => vi.mocked(api.get).mock.calls.map(([path]) => path);

afterEach(() => {
  cleanup();
  setCapabilities(null);
  vi.mocked(api.get).mockReset();
});

describe("the menu", () => {
  const register = (offered?: Capabilities) =>
    menu("Casa Doe", offered)
      .find((section) => section.id === "register")!
      .children.map((child) => ("href" in child ? child.href : "key" in child ? child.key : child.heading));

  it("links to /snap on a server, as it always has", () => {
    expect(register()).toEqual(["register", "import", "receipts", "/snap"]);
    expect(register(EVERYTHING)).toEqual(["register", "import", "receipts", "/snap"]);
  });

  it("leaves /snap out where it is not offered, and nothing else", () => {
    expect(register(DESKTOP)).toEqual(["register", "import", "receipts"]);
    const others = (offered: Capabilities) =>
      menu("Casa Doe", offered).filter((section) => section.id !== "register");
    expect(others(DESKTOP)).toEqual(others(EVERYTHING));
  });
});

describe("Admin", () => {
  it("offers invitations and resets on a server", async () => {
    answer(EVERYTHING);
    renderAdmin();
    await screen.findByText("Sam");
    expect(tabs()).toEqual(["People", "Households", "Invitations"]);
    expect(screen.getByRole("button", { name: "Reset sign-in for Sam" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reset sign-in for Alex" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Invitation link" })).toBeTruthy();
    await waitFor(() => expect(asked()).toContain("/admin/resets"));
  });

  it("offers neither on the desktop, and never asks for the resets", async () => {
    answer(DESKTOP);
    renderAdmin();
    await screen.findByText("Sam");
    await waitFor(() => expect(tabs()).toEqual(["People", "Households"]));
    expect(screen.queryByRole("button", { name: "Reset sign-in for Sam" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reset sign-in for Alex" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Invitation link" })).toBeNull();
    expect(asked()).not.toContain("/admin/resets");
  });
});

describe("loading them", () => {
  function Shown() {
    const offered = useCapabilities();
    return <p>{offered.snap ? "snap on" : "snap off"}</p>;
  }

  it("publishes what the server says to every screen already drawn", async () => {
    vi.mocked(api.get).mockResolvedValue(DESKTOP);
    render(<Shown />);
    expect(screen.getByText("snap on")).toBeTruthy();
    expect(await loadCapabilities()).toEqual(DESKTOP);
    await screen.findByText("snap off");
    expect(asked()).toEqual(["/capabilities"]);
  });

  it("assumes everything when the server cannot say, as one older than the endpoint", async () => {
    vi.mocked(api.get).mockRejectedValue(new Error("404"));
    expect(await loadCapabilities()).toEqual(EVERYTHING);
  });
});
