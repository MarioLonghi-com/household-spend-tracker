import { describe, expect, it } from "vitest";
import { CookieJar } from "../src/cookies";
import { forwardedHeaders } from "../src/scheme";

describe("what the shell forwards (#59)", () => {
  it("adds same-origin, the jar's cookies and the host, and drops the page's own", () => {
    const jar = new CookieJar();
    jar.take(["st_session=abc"]);
    const request = new Request("app://localhost/api/households", {
      method: "POST",
      headers: { "content-type": "application/json", cookie: "planted=1", host: "evil.example" },
    });
    expect(forwardedHeaders(request, jar)).toEqual([
      ["content-type", "application/json"],
      ["cookie", "st_session=abc"],
      ["host", "localhost"],
      ["sec-fetch-site", "same-origin"],
    ]);
  });

  it("leaves a request that said where it came from as it said", () => {
    const request = new Request("app://localhost/api/x", { method: "POST", headers: { "sec-fetch-site": "cross-site" } });
    const headers = forwardedHeaders(request, new CookieJar());
    expect(headers.filter(([key]) => key === "sec-fetch-site")).toEqual([["sec-fetch-site", "cross-site"]]);
  });
});
