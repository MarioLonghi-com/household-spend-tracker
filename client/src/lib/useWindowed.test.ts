// @vitest-environment jsdom

/**
 * When the window rolls back to the top.
 *
 * A new question starts from the top; an edit to one row of the same question
 * must not. The Import preview replaces its lines array whenever one line is
 * categorised, and rolling back on that would unmount the row somebody is
 * working on hundreds of lines down (#108). So it names the question itself.
 */

import { describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useWindowed } from "./useWindowed";

const rows = (count: number, tag = "") =>
  Array.from({ length: count }, (_, index) => `${tag}${index}`);

describe("useWindowed", () => {
  it("keeps how far it was unrolled when only the rows change under the same key", () => {
    const { result, rerender } = renderHook(
      ({ list, key }) => useWindowed(list, 10, key),
      { initialProps: { list: rows(35), key: "batch|line|asc" } },
    );
    act(() => result.current.extend());
    expect(result.current.shown).toBe(20);

    // One line edited: a new array, the same question.
    rerender({ list: rows(35, "edited-"), key: "batch|line|asc" });
    expect(result.current.shown).toBe(20);
    expect(result.current.visible[0]).toBe("edited-0");

    // A different sort is a new question.
    rerender({ list: rows(35, "edited-"), key: "batch|amount|desc" });
    expect(result.current.shown).toBe(10);
  });

  it("without a key, still treats a new array as a new question", () => {
    const { result, rerender } = renderHook(({ list }) => useWindowed(list, 10), {
      initialProps: { list: rows(35) },
    });
    act(() => result.current.extend());
    expect(result.current.shown).toBe(20);

    rerender({ list: rows(35, "other-") });
    expect(result.current.shown).toBe(10);
  });
});
