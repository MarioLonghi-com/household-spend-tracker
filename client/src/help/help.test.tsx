// @vitest-environment jsdom

/**
 * Help (#460): the compiler, the search, the renderer and the panel.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { blocks, compile, HelpSyntaxError, inline, split } from "./compile";
import { fold, indexArticles } from "./search";
import { HelpArticle } from "./HelpArticle";
import { HelpPanel } from "./HelpPanel";
import { helpLocaleFor, loadHelp } from "./load";
import { articlesFor, HelpContext, HelpMark } from "./HelpMark";
import { activate } from "../lib/i18n";
import { EVERYTHING, setCapabilities } from "../lib/capabilities";

afterEach(async () => {
  cleanup();
  setCapabilities(null);
  await activate("en");
});

const ARTICLE = `---
id: two-parts
title: Split a thing
screens: [register, accounts]
also:
  - divide it, in two
requires: snap
---

Open the row and press **Split**.

## Then

- one *part*
  continued
- \`code\` and <kbd>Esc</kbd>

1. see [the other](help:other) or [Accounts](screen:accounts)
`;

describe("the compiler", () => {
  it("reads the front matter, block lists included", () => {
    const { meta } = split(ARTICLE);
    expect(meta).toEqual({
      id: "two-parts",
      title: "Split a thing",
      screens: ["register", "accounts"],
      also: ["divide it, in two"],
      requires: "snap",
    });
  });

  it("builds the block tree, with every mark the format has", () => {
    const article = compile(ARTICLE);
    expect(article.blocks).toEqual([
      { t: "p", c: ["Open the row and press ", { t: "strong", c: ["Split"] }, "."] },
      { t: "h", level: 2, c: ["Then"] },
      {
        t: "ul",
        items: [
          ["one ", { t: "em", c: ["part"] }, " continued"],
          [{ t: "code", v: "code" }, " and ", { t: "kbd", v: "Esc" }],
        ],
      },
      {
        t: "ol",
        items: [
          [
            "see ",
            { t: "link", href: "help:other", c: ["the other"] },
            " or ",
            { t: "link", href: "screen:accounts", c: ["Accounts"] },
          ],
        ],
      },
    ]);
    expect(article.text).toContain("Open the row and press Split.");
  });

  it("refuses what the format does not have, rather than drawing it oddly", () => {
    expect(() => inline("<b>bold</b>")).toThrow(HelpSyntaxError);
    expect(() => inline("[x](javascript:alert(1))")).toThrow(HelpSyntaxError);
    expect(() => inline("[x](http://example.com)")).toThrow(HelpSyntaxError);
    expect(() => blocks("# A second title")).toThrow(HelpSyntaxError);
    expect(() => blocks("| a | table |")).toThrow(HelpSyntaxError);
    expect(() => split("no front matter")).toThrow(HelpSyntaxError);
    expect(() => split("---\nid: x\n---\n")).toThrow(/title/);
    expect(() => split("---\nid: x\ntitle: X\ncolour: red\n---\n")).toThrow(/colour/);
  });

  it("keeps an escaped mark as text", () => {
    expect(inline("2 \\* 3")).toEqual(["2 * 3"]);
  });
});

describe("the search", () => {
  const made = (id: string, title: string, text: string, also: string[] = []) =>
    compile(`---\nid: ${id}\ntitle: ${title}\nscreens: [register]\n${also.length ? `also:\n${also.map((a) => `  - ${a}`).join("\n")}\n` : ""}---\n\n${text}\n`);

  it("folds accents, except a language's own letters", () => {
    expect(fold("Configuración", "es-ES")).toBe("configuracion");
    expect(fold("Categoría", "pt-BR")).toBe("categoria");
    expect(fold("Växelkurs", "sv-SE")).toBe("växelkurs");
    expect(fold("Café", "sv-SE")).toBe("cafe");
  });

  it("finds an article by words typed without accents", () => {
    const index = indexArticles(
      [made("a", "Cambiar la configuración", "Abre la página."), made("b", "Dividir un movimiento", "Pulsa dividir.")],
      "es-ES",
    );
    expect(index.search("configuracion").map((one) => one.id)).toEqual(["a"]);
  });

  it("ranks the title and the other phrasings above the body", () => {
    const index = indexArticles(
      [
        made("body", "Something else", "Here a receipt is mentioned once in passing."),
        made("title", "Attach a receipt", "Open the row."),
        made("also", "Paper slips", "Open the row.", ["keep a receipt"]),
      ],
      "en",
    );
    expect(index.search("receipt").map((one) => one.id)).toEqual(["title", "also", "body"]);
  });

  it("matches the start of a word and a small typo, and ignores how-do-I words", () => {
    const index = indexArticles([made("r", "Record money owed back", "My employer pays it back.")], "en");
    expect(index.search("employ").map((one) => one.id)).toEqual(["r"]);
    expect(index.search("emplyer").map((one) => one.id)).toEqual(["r"]);
    expect(index.search("how do I")).toEqual([]);
  });

  it("answers the issue's own example from the real articles", async () => {
    const { articles } = await loadHelp("en");
    const index = indexArticles(articles, "en");
    expect(index.search("my employer owes me")[0].id).toBe("reimbursements");
    expect(index.search("split a receipt").map((one) => one.id)).toContain("split-transaction");
  });
});

describe("the languages", () => {
  it("serves each language its own articles, and English to the others", async () => {
    expect(helpLocaleFor("pt-BR")).toBe("pt-BR");
    expect(helpLocaleFor("en-XA")).toBe("en");
    const swedish = await loadHelp("sv-SE");
    const english = await loadHelp("en");
    expect(swedish.fallback).toBe(false);
    expect(swedish.articles.map((one) => one.id).sort()).toEqual(english.articles.map((one) => one.id).sort());
    expect(swedish.articles.find((one) => one.id === "split-transaction")!.title).not.toBe(
      english.articles.find((one) => one.id === "split-transaction")!.title,
    );
    expect((await loadHelp("en-XA")).fallback).toBe(true);
  });
});

describe("the article", () => {
  it("draws the tree as elements, and its links go where they say", () => {
    const opened: string[] = [];
    const { container } = render(
      <HelpArticle
        article={compile(ARTICLE)}
        onArticle={(id) => opened.push(`help:${id}`)}
        onScreen={(name) => opened.push(`screen:${name}`)}
      />,
    );
    expect(container.querySelector("h3")!.textContent).toBe("Split a thing");
    expect(container.querySelector("strong")!.textContent).toBe("Split");
    expect(container.querySelector("kbd")!.textContent).toBe("Esc");
    expect(container.querySelectorAll("ul li")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "the other" }));
    fireEvent.click(screen.getByRole("button", { name: "Accounts" }));
    expect(opened).toEqual(["help:other", "screen:accounts"]);
  });

  it("opens an outside link in a new tab, without the opener", () => {
    const article = compile("---\nid: x\ntitle: X\nscreens: [register]\n---\n\nSee [GitHub](https://example.com/x).\n");
    render(<HelpArticle article={article} onArticle={() => {}} onScreen={() => {}} />);
    const link = screen.getByRole("link", { name: "GitHub" });
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });
});

describe("the panel", () => {
  function open(props: Partial<Parameters<typeof HelpPanel>[0]> = {}) {
    const went: string[] = [];
    let closed = 0;
    render(
      <HelpPanel
        screen="register"
        onClose={() => (closed += 1)}
        onScreen={(name) => went.push(name)}
        {...props}
      />,
    );
    return { went, closed: () => closed };
  }

  it("with no question, lists the screen's articles first, then the rest", async () => {
    open({ screen: "transfers" });
    const here = await screen.findByRole("heading", { name: "About this screen" });
    const list = here.nextElementSibling as HTMLElement;
    const titles = within(list).getAllByRole("button").map((one) => one.textContent);
    expect(titles).toContain("Move money between your own accounts");
    expect(titles).toContain("How imported transfers are matched");
    expect(titles).not.toContain("Split a transaction");
    expect(screen.getByRole("heading", { name: "Every article" })).toBeTruthy();
  });

  it("answers a question, and offers the screen's articles when nothing matches", async () => {
    open({ screen: "categories" });
    const box = await screen.findByRole("searchbox", { name: "What do you want to do?" });
    fireEvent.change(box, { target: { value: "my employer owes me" } });
    expect(screen.getAllByRole("button")[1].textContent).toBe("Record money someone owes you back");
    fireEvent.change(box, { target: { value: "zzqx" } });
    expect(screen.getByRole("status").textContent).toMatch(/Nothing matched/);
    expect(screen.getByRole("heading", { name: "About this screen" })).toBeTruthy();
  });

  it("opens at an article, follows a help link, and comes back", async () => {
    open({ start: "transfers" });
    await screen.findByRole("heading", { name: "Move money between your own accounts" });
    fireEvent.click(screen.getByRole("button", { name: "how imports match transfers" }));
    await screen.findByRole("heading", { name: "How imported transfers are matched" });
    fireEvent.click(screen.getByRole("button", { name: "← Back" }));
    await screen.findByRole("heading", { name: "Move money between your own accounts" });
    fireEvent.click(screen.getByRole("button", { name: "← All articles" }));
    await screen.findByRole("searchbox");
  });

  it("the old How import works route's article names every outcome in the preview's words", async () => {
    open({ screen: "import", start: "import-how-it-works" });
    const article = (await screen.findByRole("heading", { name: "How import reads a statement" })).closest("article")!;
    const labels = [...article.querySelectorAll("li > strong:first-child")].map((one) => one.textContent);
    expect(labels).toEqual(
      expect.arrayContaining(["New", "Already have it", "Already imported", "Needs a look", "Could not read", "Not for this account"]),
    );
    // Information only: nothing on it that changes anything.
    expect(article.querySelectorAll("input, select, textarea")).toHaveLength(0);
  });

  it("a screen link opens the screen and closes the panel", async () => {
    const seen = open({ start: "account-identifiers" });
    const article = (await screen.findByRole("heading", { name: "Tell the app what your bank calls an account" })).closest("article")!;
    fireEvent.click(within(article).getAllByRole("button", { name: "Accounts" })[0]);
    expect(seen.went).toEqual(["accounts"]);
    expect(seen.closed()).toBe(1);
  });

  it("leaves out an article about something this deployment does not offer", async () => {
    setCapabilities({ ...EVERYTHING, snap: false });
    open({ screen: "receipts" });
    await screen.findByRole("heading", { name: "About this screen" });
    expect(screen.queryByRole("button", { name: "Photograph a receipt with your phone" })).toBeNull();
    expect(screen.getByRole("button", { name: "Attach a receipt to a transaction" })).toBeTruthy();
  });

  it("follows a change of language while open", async () => {
    open({ start: "split-transaction" });
    await screen.findByRole("heading", { name: "Split a transaction" });
    await activate("pt-BR");
    const portuguese = (await loadHelp("pt-BR")).articles.find((one) => one.id === "split-transaction")!;
    await screen.findByRole("heading", { name: portuguese.title });
    expect(document.querySelector('[lang="en"]')).toBeNull();
  });

  it("in a language with no articles, shows English marked as English, and says why", async () => {
    await activate("en-XA");
    open({ start: "split-transaction" });
    await screen.findByRole("heading", { name: "Split a transaction" });
    expect(document.querySelector('.help-panel[lang="en"]')).toBeTruthy();
    expect(document.querySelector(".banner.info")!.textContent).not.toMatch(/^[\x20-\x7e]+$/);
  });
});

describe("the ? beside a screen's title", () => {
  it("is drawn on a screen with articles, and opens help", () => {
    let opened = 0;
    render(
      <HelpContext.Provider value={{ screen: "register", open: () => (opened += 1) }}>
        <h1>
          Transactions
          <HelpMark />
        </h1>
      </HelpContext.Provider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Help with this screen" }));
    expect(opened).toBe(1);
  });

  it("is not drawn where there is no article, nor outside the shell", () => {
    render(
      <HelpContext.Provider value={{ screen: "history", open: () => {} }}>
        <HelpMark />
      </HelpContext.Provider>,
    );
    expect(articlesFor("history", EVERYTHING)).toEqual([]);
    expect(screen.queryByRole("button")).toBeNull();
    cleanup();
    render(<HelpMark />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("knows a screen's articles from the index, as the deployment offers them", () => {
    expect(articlesFor("receipts", EVERYTHING)).toEqual(expect.arrayContaining(["attach-receipt", "snap-receipt"]));
    expect(articlesFor("receipts", { ...EVERYTHING, snap: false })).not.toContain("snap-receipt");
  });
});
