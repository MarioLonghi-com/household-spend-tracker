// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { activate } from "./i18n";
import { codedText, noticeText } from "./noticeMessages";

afterEach(async () => {
  await activate("en");
});

const transfer = {
  code: "import.line.maybe_transfer",
  params: {
    account: "Savings",
    date: "2026-01-02",
    why: { code: "transfer.why.more_than_one", params: { why: { code: "transfer.why.amounts_match", params: {} } } },
  },
};

describe("an import's sentences (#267)", () => {
  it("are the server's English in English, whatever the code says", () => {
    expect(noticeText("As the server said", transfer)).toBe("As the server said");
    expect(codedText("As the server said", "import.line.no_money", {})).toBe("As the server said");
  });

  it("are worded from the code in another language, a sentence inside a sentence included", async () => {
    await activate("en-XA");
    const said = noticeText("As the server said", transfer);
    expect(said).not.toBe("As the server said");
    expect(said).toContain("Savings");
    expect(said).toContain("2026-01-02");
    expect(said).not.toMatch(/\bamounts match\b/);
  });

  it("format an amount from minor units in its currency", async () => {
    await activate("en-XA");
    const said = codedText("English", "import.note.balance_gap", {
      opening: 123456,
      held: -1000,
      gap: 124456,
      date: "2026-02-01",
      currency: "EUR",
    });
    expect(said).toMatch(/1[,.]?234[.,]56/);
    expect(said).not.toContain("123456");
  });

  it("fall back to the English for a code this build does not know, or a part it cannot word", async () => {
    await activate("en-XA");
    expect(codedText("English", "import.line.from_the_future", {})).toBe("English");
    expect(noticeText("English", { code: "transfer.why.but", params: { why: { code: "nope" }, payer: "x" } })).toBe(
      "English",
    );
    expect(codedText("English", "import.note.balance_gap", { opening: 1, held: 2, gap: 3, date: "2026-02-01" })).toBe(
      "English",
    );
  });
});

describe("a refusal whose code is a notice's (#267)", () => {
  it("is worded from the notice outside English, and is the server's sentence in English", async () => {
    const { ApiError } = await import("./api");
    const { problemText } = await import("./errorMessages");
    const refused = new ApiError("the server's sentence", 422, undefined, {
      detail: "the server's sentence",
      code: "statement.unreadable.too_many_rows",
      params: { max: 100000 },
    });
    expect(problemText(refused)).toBe("the server's sentence");
    await activate("en-XA");
    const said = problemText(refused);
    expect(said).not.toBe("the server's sentence");
    expect(said).toMatch(/100[,.  ]?000/);
  });
});
