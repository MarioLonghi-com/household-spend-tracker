// @vitest-environment jsdom
/**
 * The client-side halves of the QA pass (#271): words the server sends in
 * English, said by the client; and the `lang` an amount carries when it is
 * written in another language than the screen's.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { Money } from "../components/bits";
import { activate, isReachable } from "./i18n";
import { categoryName, logStreamBlurb, logStyleWords, placeWords } from "./labels";
import { amountLang, setFormatLocale } from "./locale";

afterEach(async () => {
  cleanup();
  setFormatLocale("en-US");
  await activate("en");
});

describe("Application management's words", () => {
  it("are the server's English, letter for letter, in English", () => {
    expect(placeWords({ what: "Secret key", note: "back this up with the database — without it every session and every authenticator is void" })).toEqual({
      what: "Secret key",
      note: "back this up with the database — without it every session and every authenticator is void",
    });
    expect(logStreamBlurb({ key: "access", blurb: "server's" })).toBe(
      "One line per HTTP request. Mechanical, and it drowns everything else.",
    );
    expect(logStyleWords({ key: "quiet", label: "server's", blurb: "server's" })).toEqual({
      label: "Quiet",
      blurb: "Only what went wrong. A healthy instance writes almost nothing.",
    });
  });

  it("read as the server sent them when this build does not know them", () => {
    expect(placeWords({ what: "Something new", note: "a new note" })).toEqual({ what: "Something new", note: "a new note" });
    expect(logStreamBlurb({ key: "new", blurb: "as sent" })).toBe("as sent");
    expect(logStyleWords({ key: "new", label: "New", blurb: "as sent" })).toEqual({ label: "New", blurb: "as sent" });
  });

  it("name the row with no category in the reader's language, and others as stored", async () => {
    expect([categoryName(null, "Uncategorised"), categoryName("c1", "Groceries")]).toEqual(["Uncategorised", "Groceries"]);
    await activate("en-XA");
    expect(categoryName(null, "Uncategorised")).not.toBe("Uncategorised");
    expect(categoryName("c1", "Groceries")).toBe("Groceries");
  });
});

describe("an amount's lang", () => {
  it("is nothing while the format and the words share a language", () => {
    setFormatLocale("en-GB");
    expect(amountLang()).toBeUndefined();
    const { container } = render(<Money minor={123456} currency="EUR" />);
    expect(container.querySelector(".amount")!.hasAttribute("lang")).toBe(false);
  });

  it("is the format's own when the words are in another language", async () => {
    setFormatLocale("sv-SE");
    expect(amountLang()).toBe("sv-SE");
    const { container } = render(<Money minor={123456} currency="SEK" />);
    const amount = container.querySelector(".amount")!;
    expect([amount.getAttribute("lang"), amount.textContent]).toEqual(["sv-SE", "1 234,56 kr"]);
  });
});

describe("the drafts outside a QA build", () => {
  it.each(["pt-BR", "es-ES", "sv-SE"])("%s cannot be reached", (locale) => {
    expect(isReachable(locale)).toBe(false);
  });
});
