/**
 * Before every test file.
 *
 * - The formatting locale pinned to en-US (#52). `lib/locale.ts` follows the
 *   browser's locale, and under Node that is whatever the machine running the
 *   tests was set to. A test that expects "€1,234.56" must not pass on one
 *   laptop and fail on another. A test about another locale pins its own.
 * - Every `render` inside Lingui's `I18nProvider` (#53), as `main.tsx` renders
 *   the app. A component that reads the language -- `Problem`, `SortHeading`,
 *   anything with a `<Trans>` -- needs the provider, and a test should not have
 *   to remember it. A test's own `wrapper` still applies, inside it.
 */
import { createElement, type ComponentType, type ReactNode } from "react";
import { vi } from "vitest";
import { I18nProvider } from "@lingui/react";
import { i18n } from "./lib/i18n";
import { setFormatLocale } from "./lib/locale";

setFormatLocale("en-US");

vi.mock("@testing-library/react", async (importOriginal) => {
  const library = await importOriginal<typeof import("@testing-library/react")>();
  type Options = Parameters<typeof library.render>[1];
  function withLingui(inner?: ComponentType<{ children: ReactNode }>) {
    return function LinguiWrapper({ children }: { children: ReactNode }) {
      const content = inner ? createElement(inner, null, children) : children;
      return createElement(I18nProvider, { i18n }, content);
    };
  }
  const render = ((ui: Parameters<typeof library.render>[0], options?: Options) =>
    library.render(ui, {
      ...options,
      wrapper: withLingui(options?.wrapper as ComponentType<{ children: ReactNode }> | undefined),
    } as Options)) as typeof library.render;
  const renderHook = ((callback: Parameters<typeof library.renderHook>[0], options?: Parameters<typeof library.renderHook>[1]) =>
    library.renderHook(callback, {
      ...options,
      wrapper: withLingui(options?.wrapper as ComponentType<{ children: ReactNode }> | undefined),
    })) as typeof library.renderHook;
  return { ...library, render, renderHook };
});
