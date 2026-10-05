// @vitest-environment jsdom

/**
 * A drag that ends outside a panel does not close it (#27).
 *
 * The browser sends `click` to the nearest element holding both the press and
 * the release, so a press in a field dragged out past the panel's edge lands
 * its click on the backdrop. The backdrop closes only when the press started
 * on it too. Asserted by what is still on screen and what the field holds, not
 * by whether a handler ran.
 */

import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { Dialog, Panel } from "./bits";

afterEach(cleanup);

/** A panel with a field in it, the way every editing panel is. */
function WithPanel({ onClose }: { onClose?: () => void }) {
  const [open, setOpen] = useState(true);
  const [memo, setMemo] = useState("");
  if (!open) return <p>closed</p>;
  return (
    <Panel
      title="Edit"
      onClose={() => {
        onClose?.();
        setOpen(false);
      }}
    >
      <label>
        Memo
        <input value={memo} onChange={(event) => setMemo(event.target.value)} />
      </label>
    </Panel>
  );
}

function backdropOf(dialog: HTMLElement) {
  const backdrop = dialog.parentElement;
  if (!backdrop) throw new Error("no backdrop");
  return backdrop;
}

describe("Panel backdrop", () => {
  it("stays open, and keeps what was typed, when a press in the field ends on the backdrop", () => {
    render(<WithPanel />);
    const field = screen.getByLabelText("Memo") as HTMLInputElement;
    fireEvent.change(field, { target: { value: "lunch with Bea" } });

    const backdrop = backdropOf(screen.getByRole("dialog", { name: "Edit" }));
    fireEvent.pointerDown(field);
    fireEvent.mouseDown(field);
    fireEvent.mouseUp(backdrop);
    fireEvent.click(backdrop);

    expect(screen.getByRole("dialog", { name: "Edit" })).toBeTruthy();
    expect((screen.getByLabelText("Memo") as HTMLInputElement).value).toBe("lunch with Bea");
    expect(screen.queryByText("closed")).toBeNull();
  });

  it("closes when the press and the click are both on the backdrop", () => {
    const onClose = vi.fn();
    render(<WithPanel onClose={onClose} />);
    const backdrop = backdropOf(screen.getByRole("dialog", { name: "Edit" }));

    fireEvent.pointerDown(backdrop);
    fireEvent.mouseDown(backdrop);
    fireEvent.mouseUp(backdrop);
    fireEvent.click(backdrop);

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("closed")).toBeTruthy();
  });

  it("does not close on a click that no press on the backdrop started", () => {
    const onClose = vi.fn();
    render(<WithPanel onClose={onClose} />);
    const backdrop = backdropOf(screen.getByRole("dialog", { name: "Edit" }));

    // A press on the backdrop, then one inside: only the latest press counts.
    fireEvent.mouseDown(backdrop);
    fireEvent.mouseDown(screen.getByLabelText("Memo"));
    fireEvent.click(backdrop);

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Edit" })).toBeTruthy();
  });

  it("still closes on Escape", () => {
    render(<WithPanel />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("closed")).toBeTruthy();
  });

  it("still closes on ✕", () => {
    render(<WithPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("closed")).toBeTruthy();
  });
});

describe("Dialog backdrop", () => {
  function WithDialog() {
    const [open, setOpen] = useState(true);
    const [note, setNote] = useState("");
    if (!open) return <p>closed</p>;
    return (
      <Dialog title="Ask" onClose={() => setOpen(false)}>
        <label>
          Note
          <input value={note} onChange={(event) => setNote(event.target.value)} />
        </label>
      </Dialog>
    );
  }

  it("stays open when a press inside ends on the backdrop", () => {
    render(<WithDialog />);
    const field = screen.getByLabelText("Note") as HTMLInputElement;
    fireEvent.change(field, { target: { value: "keep me" } });
    const backdrop = backdropOf(screen.getByRole("dialog", { name: "Ask" }));

    fireEvent.pointerDown(field);
    fireEvent.mouseDown(field);
    fireEvent.click(backdrop);

    expect((screen.getByLabelText("Note") as HTMLInputElement).value).toBe("keep me");
  });

  it("closes when the press and the click are both on the backdrop", () => {
    render(<WithDialog />);
    const backdrop = backdropOf(screen.getByRole("dialog", { name: "Ask" }));

    fireEvent.pointerDown(backdrop);
    fireEvent.mouseDown(backdrop);
    fireEvent.click(backdrop);

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("closed")).toBeTruthy();
  });
});
