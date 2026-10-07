// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { openedAt, putTheAddressBack } from "./App";

describe("opening a screen in a new tab", () => {
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("opens the named screen and household, then puts the address back", () => {
    window.history.replaceState(null, "", "/?open=history&household=hh-2");
    const opened = openedAt();
    expect(opened).toEqual({ screen: "history", household: "hh-2" });
    putTheAddressBack(opened);
    expect(window.location.search).toBe("");
  });

  it("gives the same answer when asked twice, as Strict Mode asks it (#110)", () => {
    window.history.replaceState(null, "", "/?open=accounts&household=hh-2");
    expect(openedAt()).toEqual({ screen: "accounts", household: "hh-2" });
    expect(openedAt()).toEqual({ screen: "accounts", household: "hh-2" });
    expect(window.location.search).toBe("?open=accounts&household=hh-2");
  });

  it("opens the payee rules, which the One-time Import's report points at (#265)", () => {
    window.history.replaceState(null, "", "/?open=rules&household=hh-2");
    expect(openedAt()).toEqual({ screen: "rules", household: "hh-2" });
  });

  it("ignores a screen it was not built to open", () => {
    window.history.replaceState(null, "", "/?open=settings&household=hh-2");
    const opened = openedAt();
    expect(opened).toEqual({ screen: null, household: "" });
    putTheAddressBack(opened);
    expect(window.location.search).toBe("?open=settings&household=hh-2");
  });

  it("drops a household that is not an id (#197)", () => {
    for (const bad of ["../me", "..%2Fme%3Fx%3D", "a/b", "x".repeat(65)]) {
      window.history.replaceState(null, "", `/?open=accounts&household=${bad}`);
      expect(openedAt()).toEqual({ screen: "accounts", household: "" });
    }
  });
});
