import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { loadNotices, NOTICES_FILE, noticesPath, page, REPOSITORY, sourceLink, type Words } from "../src/licences";

describe("About → Licences (#63)", () => {
  it("links the tree exactly as tagged for this version", () => {
    expect(sourceLink("0.15.0")).toBe("https://github.com/MarioLonghi-com/household-spend-tracker/tree/v0.15.0");
    expect(sourceLink(" 1.2.3-rc.1 ")).toBe(`${REPOSITORY}/tree/v1.2.3-rc.1`);
    expect(() => sourceLink("45.2.1 ")).not.toThrow();
    expect(() => sourceLink("")).toThrow(/release version/);
    expect(() => sourceLink("v0.15.0")).toThrow(/release version/);
    expect(() => sourceLink("0.15")).toThrow(/release version/);
  });

  it("looks for the notices in the resources folder when packaged, in build/ from source", () => {
    expect(noticesPath("/Applications/App.app/Contents/Resources")).toBe(path.join("/Applications/App.app/Contents/Resources", NOTICES_FILE));
    expect(noticesPath(null, "/repo/desktop/build")).toBe(path.join("/repo/desktop/build", "..", "..", "build", NOTICES_FILE));
  });

  it("reads the notices, and says so when they are missing or empty rather than showing nothing", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "st-licences-"));
    expect(loadNotices(path.join(dir, "absent.txt"))).toBeNull();
    fs.writeFileSync(path.join(dir, "empty.txt"), "  \n");
    expect(loadNotices(path.join(dir, "empty.txt"))).toBeNull();
    fs.writeFileSync(path.join(dir, "notices.txt"), "alembic 1.20.1  --  MIT\nCopyright Michael Bayer\n");
    expect(loadNotices(path.join(dir, "notices.txt"))).toBe("alembic 1.20.1  --  MIT\nCopyright Michael Bayer\n");
  });

  const words: Words = {
    lang: "sv-SE",
    dark: false,
    title: "Licenser",
    free: "Fri programvara <AGPL>",
    sourceLabel: "Källkoden för den här versionen",
    link: sourceLink("0.15.0"),
    noticesHeading: "Tredjepartsmeddelanden",
    notices: 'react 19.3.1  --  MIT\n<script>alert("x")</script> & "quotes"',
    missing: "Meddelandena följde inte med den här versionen.",
    where: "I appens resursmapp: /Resources",
  };

  it("renders every word escaped, the link once as a link, and the notices verbatim in a <pre>", () => {
    const html = page(words);
    expect(html).toContain('<html lang="sv-SE">');
    expect(html).toContain("<h1>Licenser</h1>");
    expect(html).toContain("Fri programvara &#60;AGPL&#62;");
    expect(html).toContain(`<a href="${words.link}">${words.link}</a>`);
    expect(html).toContain("<pre>react 19.3.1  --  MIT\n&#60;script&#62;alert(&#34;x&#34;)&#60;/script&#62; &#38; &#34;quotes&#34;</pre>");
    expect(html).not.toContain("<script>");
    expect(html).not.toContain(words.missing);
    expect(html).toContain("default-src 'none'");
  });

  it("shows the missing line, and no <pre>, when the notices did not ship", () => {
    const html = page({ ...words, notices: null, dark: true });
    expect(html).toContain("<p>Meddelandena följde inte med den här versionen.</p>");
    expect(html).not.toContain("<pre>");
    expect(html).toContain("#1e1f22");
  });
});
