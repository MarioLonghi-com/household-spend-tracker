/**
 * The pseudo-locale's tag, on its own so a module that only needs the tag
 * (`locale.ts`, and through it `money.ts`) does not load `i18n.ts` and its
 * catalogs: `tests/test_client_agrees.py` runs `money.ts` under tsx, which
 * cannot import a `.po` file.
 */
/** Reachable by storing it, never offered: the pseudo-locale. */
export const PSEUDO_LOCALE = "en-XA";
