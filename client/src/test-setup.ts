/**
 * Before every test file: the formatting locale pinned to en-US (#52).
 *
 * `lib/locale.ts` follows the browser's locale, and under Node that is
 * whatever the machine running the tests was set to. A test that expects
 * "€1,234.56" must not pass on one laptop and fail on another. A test about
 * another locale passes it explicitly or pins its own.
 */
import { setFormatLocale } from "./lib/locale";

setFormatLocale("en-US");
