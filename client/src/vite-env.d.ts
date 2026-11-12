/// <reference types="vite/client" />

// The reference Vite's own scaffold ships and this client never had. It pulls
// in the ambient declarations for the things Vite lets you import that are not
// TypeScript -- `*.css` among them, which `main.tsx` imports for its
// side-effect.
//
// TypeScript 5 did not ask. TypeScript 7 does: a side-effect import of a
// module with no declaration is TS2882, so `import "./styles.css"` failed the
// typecheck while building perfectly well. The stylesheet was never untyped by
// intent; the file that says so was just missing.

// A Lingui catalog, compiled by `@lingui/vite-plugin` when it is imported (#53).
declare module "*.po" {
  import type { Messages } from "@lingui/core";
  export const messages: Messages;
}

// The help articles, compiled by `help-articles.ts` (#460): one language's
// articles, and which screens each is about.
declare module "virtual:help-articles/*" {
  import type { Article } from "./help/compile";
  const articles: Article[];
  export default articles;
}
declare module "virtual:help-index" {
  const index: Record<string, { screens: string[]; requires?: string }>;
  export default index;
}
