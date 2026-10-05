// @vitest-environment jsdom

/**
 * A panel closes on a click outside it -- and only on one (#27).
 *
 * A click goes to the nearest element holding both the press and the release.
 * Selecting text in a field and letting go past the panel's edge is therefore
 * a click on the backdrop, and it closed the panel with what was typed in it.
 */

import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Dialog, Panel } from "./bits";

afterEach(cleanup);

function Holder({ dirty = false }: { dirty?: boolean }) {
  const [open, setOpen] = useState(true);
  const [memo, setMemo] = useState("");
  if (!open) return <p>closed</p>;
  return (
    <Panel title="Edit" onClose={() => setOpen(false)} dirty={dirty}>
      <input aria-label="Memo" value={memo} onChange={(e) => setMemo(e.target.value)} />
    </Panel>
  );
}

const backdrop = () => document.querySelector(".panel-backdrop") as HTMLElement;

describe("the panel backdrop", () => {
  it("keeps the panel and what was typed when a press inside ends outside", () => {
    render(<Holder />);
    const memo = screen.getByLabelText("Memo");
    fireEvent.change(memo, { target: { value: "Shared lunch" } });

    fireEvent.pointerDown(memo);
    fireEvent.click(backdrop());

    expect(screen.queryByText("closed")).toBeNull();
    expect((screen.getByLabelText("Memo") as HTMLInputElement).value).toBe("Shared lunch");
  });

  it("still closes on a press and a click both outside", () => {
    render(<Holder />);
    fireEvent.pointerDown(backdrop());
    fireEvent.click(backdrop());
    expect(screen.getByText("closed")).toBeTruthy();
  });

  it("does not close a dirty panel from the backdrop at all", () => {
    render(<Holder dirty />);
    fireEvent.pointerDown(backdrop());
    fireEvent.click(backdrop());
    expect(screen.queryByText("closed")).toBeNull();
  });

  it("still lets Escape through on a dirty panel, for the caller to ask", () => {
    render(<Holder dirty />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.getByText("closed")).toBeTruthy();
  });
});

function DialogHolder() {
  const [open, setOpen] = useState(true);
  const [reason, setReason] = useState("");
  if (!open) return <p>closed</p>;
  return (
    <Dialog title="Reset sign-in?" onClose={() => setOpen(false)}>
      <input aria-label="Reason" value={reason} onChange={(e) => setReason(e.target.value)} />
    </Dialog>
  );
}

const dialogBackdrop = () => document.querySelector(".dialog-backdrop") as HTMLElement;

describe("the confirmation dialog's backdrop", () => {
  it("keeps the dialog and what was typed when a press inside ends outside", () => {
    render(<DialogHolder />);
    const reason = screen.getByLabelText("Reason");
    fireEvent.change(reason, { target: { value: "lost phone" } });

    fireEvent.pointerDown(reason);
    fireEvent.click(dialogBackdrop());

    expect(screen.queryByText("closed")).toBeNull();
    expect((screen.getByLabelText("Reason") as HTMLInputElement).value).toBe("lost phone");
  });

  it("still closes on a press and a click both outside", () => {
    render(<DialogHolder />);
    fireEvent.pointerDown(dialogBackdrop());
    fireEvent.click(dialogBackdrop());
    expect(screen.getByText("closed")).toBeTruthy();
  });
});
