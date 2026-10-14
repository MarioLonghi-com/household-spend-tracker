import { describe, expect, it } from "vitest";
import { CookieJar } from "../src/cookies";

describe("the cookie jar (#59, #320)", () => {
  it("keeps what the backend set and sends it back", () => {
    const jar = new CookieJar(() => 1_000_000);
    jar.take(["st_session=abc; Path=/; HttpOnly; SameSite=Strict", "st_device=xyz; Max-Age=600"]);
    expect(jar.header()).toBe("st_session=abc; st_device=xyz");
  });

  it("forgets one the backend cleared, and one that expired", () => {
    let now = 1_000_000;
    const jar = new CookieJar(() => now);
    jar.take(["st_session=abc", "st_device=xyz; Max-Age=60"]);
    jar.take(["st_session=; Max-Age=0"]);
    expect(jar.names()).toEqual(["st_device"]);
    now += 61_000;
    expect(jar.header()).toBe("");
  });

  it("lets Max-Age win over Expires, as a browser does", () => {
    const jar = new CookieJar(() => Date.parse("2026-10-13T12:00:00Z"));
    jar.take(["a=1; Expires=Thu, 01 Jan 2026 00:00:00 GMT; Max-Age=3600"]);
    expect(jar.header()).toBe("a=1");
  });

  it("keeps a value with an equals sign in it whole", () => {
    const jar = new CookieJar();
    jar.take(["token=a=b=c; Path=/"]);
    expect(jar.header()).toBe("token=a=b=c");
  });

  it("is empty after clear, which a restore does", () => {
    const jar = new CookieJar();
    jar.take(["st_session=abc"]);
    jar.clear();
    expect(jar.header()).toBe("");
  });
});
