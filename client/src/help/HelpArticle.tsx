/**
 * One help article, drawn from the block tree the build made of it (#460).
 *
 * Every element is React's own: no `dangerouslySetInnerHTML`, no Markdown
 * parsed here. A `help:` link opens another article in the panel, a
 * `screen:` link opens that screen of the app (and the shell closes the
 * panel), and an `https://` link opens in a new tab.
 */

import type { ReactNode } from "react";
import type { Article, Block, Inline } from "./compile";

type Links = {
  onArticle: (id: string) => void;
  onScreen: (screen: string) => void;
};

function words(content: Inline[], links: Links): ReactNode[] {
  return content.map((one, at) => {
    if (typeof one === "string") return one;
    switch (one.t) {
      case "strong":
        return <strong key={at}>{words(one.c, links)}</strong>;
      case "em":
        return <em key={at}>{words(one.c, links)}</em>;
      case "code":
        return <code key={at}>{one.v}</code>;
      case "kbd":
        return <kbd key={at}>{one.v}</kbd>;
      case "link": {
        const { href } = one;
        if (href.startsWith("help:") || href.startsWith("screen:")) {
          const [kind, target] = [href.slice(0, href.indexOf(":")), href.slice(href.indexOf(":") + 1)];
          return (
            <button
              type="button"
              className="link"
              key={at}
              onClick={() => (kind === "help" ? links.onArticle(target) : links.onScreen(target))}
            >
              {words(one.c, links)}
            </button>
          );
        }
        return (
          <a key={at} href={href} target="_blank" rel="noopener noreferrer">
            {words(one.c, links)}
          </a>
        );
      }
    }
  });
}

function block(one: Block, at: number, links: Links): ReactNode {
  switch (one.t) {
    case "h":
      return one.level === 2 ? <h4 key={at}>{words(one.c, links)}</h4> : <h5 key={at}>{words(one.c, links)}</h5>;
    case "p":
      return <p key={at}>{words(one.c, links)}</p>;
    case "ul":
    case "ol": {
      const items = one.items.map((item, n) => <li key={n}>{words(item, links)}</li>);
      return one.t === "ul" ? <ul key={at}>{items}</ul> : <ol key={at}>{items}</ol>;
    }
  }
}

export function HelpArticle({ article, ...links }: { article: Article } & Links) {
  return (
    <article className="help-article">
      <h3>{article.title}</h3>
      {article.blocks.map((one, at) => block(one, at, links))}
    </article>
  );
}
