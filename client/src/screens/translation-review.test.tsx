// @vitest-environment jsdom

/**
 * Review mode (#272): an owner, on their own device, reviews the drafts.
 *
 * Off -- the default, and always for a member -- the languages cannot be
 * chosen and nothing English changes. On, Profile's picker offers each draft
 * as a preview, a draft can be shown, and "Suggest a better wording" sends
 * the reviewer's words with the message they are for.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { LanguagePicker } from "../components/LanguagePicker";
import { SuggestWording } from "../components/SuggestWording";
import { activate, i18n, isReachable, LOCALE_KEY, reviewing, setReviewer, storedLocale } from "../lib/i18n";
import { REVIEW_KEY, reviewAllowed, reviewStored } from "../lib/review";
import type { Household } from "../lib/types";
import { TranslationReview, type TranslationSuggestion } from "./TranslationReview";

const HOUSEHOLD: Household = {
  id: "house-1",
  name: "Ours",
  base_currency: "EUR",
  date_format: "YYYY-MM-DD",
  note: null,
  theme: "default",
  accent: null,
  receipts_keep_original: false,
  receipts_keep_original_forced: false,
  receipts_with_original: 0,
  colours: null,
};

function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
});

afterEach(async () => {
  cleanup();
  await setReviewer(false);
  await activate("en");
  window.localStorage.clear();
});

describe("with review mode off", () => {
  it("offers no language, stored or asked for, to an owner or a member", async () => {
    window.localStorage.setItem(LOCALE_KEY, "pt-BR");
    expect([reviewAllowed("owner"), reviewAllowed("member")]).toEqual([false, false]);
    await setReviewer(reviewAllowed("owner"));
    expect([reviewing(), isReachable("pt-BR"), storedLocale(), await activate("pt-BR")]).toEqual([
      false,
      false,
      null,
      "en",
    ]);
    const { container } = render(<LanguagePicker />);
    expect(container.innerHTML).toBe("");
  });

  it("is off for a member even when the device has the switch on", () => {
    window.localStorage.setItem(REVIEW_KEY, "on");
    expect([reviewStored(), reviewAllowed("member"), reviewAllowed("owner")]).toEqual([true, false, true]);
  });

  it("shows an owner the switch, off, and nothing else", () => {
    render(wrap(<TranslationReview household={HOUSEHOLD} />));
    const box = screen.getByRole("checkbox", { name: /Review translations on this device/ });
    expect((box as HTMLInputElement).checked).toBe(false);
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("link", { name: /JSON/ })).toBeNull();
    expect(api.get).not.toHaveBeenCalled();
  });
});

describe("with review mode on", () => {
  it("offers each draft as a preview beside English, and shows the one chosen", async () => {
    await setReviewer(true);
    render(<LanguagePicker />);
    const options = screen.getAllByRole("option").map((one) => one.textContent);
    expect(options).toEqual([
      "English",
      "português (Brasil) — Preview — machine translated, under review",
      "español de España — Preview — machine translated, under review",
      "svenska (Sverige) — Preview — machine translated, under review",
    ]);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "sv-SE" } });
    await waitFor(() => expect(i18n.locale).toBe("sv-SE"));
    expect([document.documentElement.lang, window.localStorage.getItem(LOCALE_KEY)]).toEqual(["sv-SE", "sv-SE"]);
  });

  it("puts a draft back to English when it is turned off, and brings it back when on", async () => {
    window.localStorage.setItem(LOCALE_KEY, "es-ES");
    await setReviewer(true);
    expect(i18n.locale).toBe("es-ES");
    await setReviewer(false);
    expect([i18n.locale, isReachable("es-ES")]).toEqual(["en", false]);
  });

  it("turns on from the switch, which is kept on this device", async () => {
    vi.mocked(api.get).mockResolvedValue([] satisfies TranslationSuggestion[]);
    render(wrap(<TranslationReview household={HOUSEHOLD} />));
    fireEvent.click(screen.getByRole("checkbox", { name: /Review translations on this device/ }));
    await waitFor(() => expect(reviewing()).toBe(true));
    expect(window.localStorage.getItem(REVIEW_KEY)).toBe("on");
    expect(await screen.findByText(/No suggestions yet/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "All languages (JSON)" }).getAttribute("href")).toBe(
      "/api/households/house-1/translation-suggestions/export.json",
    );
  });

  it("lists the suggestions sorted at their headers, and marks one applied", async () => {
    await setReviewer(true);
    const two: TranslationSuggestion[] = [
      { id: "s1", locale: "sv-SE", context: "", message: "Save", suggested: "Spara", note: null,
        suggested_by_id: "u1", status: "open", created_at: "2026-10-01T10:00:00" },
      { id: "s2", locale: "pt-BR", context: "", message: "Cancel", suggested: "Cancelar", note: "shorter",
        suggested_by_id: "u1", status: "applied", created_at: "2026-10-02T10:00:00" },
    ];
    vi.mocked(api.get).mockResolvedValue(two);
    vi.mocked(api.post).mockResolvedValue([]);
    render(wrap(<TranslationReview household={HOUSEHOLD} />));
    const table = await screen.findByRole("table");
    const firstColumn = () =>
      within(table).getAllByRole("row").slice(1).map((row) => row.querySelector("td")!.textContent);
    // Newest first, then by language name when that heading is chosen.
    expect(firstColumn()).toEqual(["português (Brasil)", "svenska (Sverige)"]);
    fireEvent.click(within(table).getByRole("button", { name: /English/ }));
    expect(firstColumn()).toEqual(["português (Brasil)", "svenska (Sverige)"]);
    fireEvent.click(within(table).getByRole("button", { name: /English/ }));
    expect(firstColumn()).toEqual(["svenska (Sverige)", "português (Brasil)"]);

    fireEvent.click(within(table).getByRole("button", { name: "Mark applied" }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/households/house-1/translation-suggestions/status", {
        ids: ["s1"],
        status: "applied",
      }),
    );
  });
});

describe("suggesting a better wording", () => {
  it("finds a message by its English, shows its note and draft, and sends the new words", async () => {
    await setReviewer(true);
    await activate("pt-BR");
    vi.mocked(api.post).mockResolvedValue({});
    render(wrap(<SuggestWording household={HOUSEHOLD} locale="pt-BR" />));
    fireEvent.click(screen.getByRole("button", { name: "Sugerir uma redação melhor" }));
    fireEvent.change(await screen.findByRole("searchbox"), { target: { value: "Snap a Receipt" } });
    fireEvent.click(await screen.findByRole("button", { name: "Fotografar um comprovante" }));

    const words = screen.getByRole("textbox", { name: "Sua redação" });
    expect((words as HTMLTextAreaElement).value).toBe("Fotografar um comprovante");
    fireEvent.change(words, { target: { value: "Fotografe um comprovante" } });
    fireEvent.change(screen.getByRole("textbox", { name: /Por quê/ }), { target: { value: "  imperativo  " } });
    fireEvent.click(screen.getByRole("button", { name: "Salvar a sugestão" }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/households/house-1/translation-suggestions", {
        locale: "pt-BR",
        message: "Snap a Receipt",
        context: "",
        suggested: "Fotografe um comprovante",
        note: "imperativo",
      }),
    );
  });

  it("will not save words that lose a placeholder", async () => {
    await setReviewer(true);
    await activate("sv-SE");
    render(wrap(<SuggestWording household={HOUSEHOLD} locale="sv-SE" />));
    fireEvent.click(screen.getByRole("button", { name: /Föreslå en bättre formulering/ }));
    fireEvent.change(await screen.findByRole("searchbox"), { target: { value: "Rename {0}" } });
    const match = await screen.findAllByRole("button", { name: /\{0\}/ });
    fireEvent.click(match[0]);
    const words = screen.getByRole("textbox", { name: "Din formulering" });
    fireEvent.change(words, { target: { value: "Byt namn" } });
    expect(screen.getByRole("alert").textContent).toContain("{0}");
    expect((screen.getByRole("button", { name: "Spara förslaget" }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.post).not.toHaveBeenCalled();
  });
});
