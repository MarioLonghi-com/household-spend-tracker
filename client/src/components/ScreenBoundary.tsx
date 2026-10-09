/**
 * One broken screen must not take the navigation with it (#193).
 *
 * React 19 unmounts the whole root when a render throws and nothing catches
 * it, so a single row the formatter could not read left a blank page -- and
 * the screen that could repair it was one of the blank ones. This sits around
 * `<main>` only: the menu stays, and going to another screen (or household)
 * is a fresh start rather than the same error again.
 */

import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { Problem } from "./bits";
import { Trans } from "@lingui/react/macro";

type Props = {
  /** Whatever names what is on screen. A new value clears a caught error. */
  resetKey: string;
  children: ReactNode;
};

type State = { error: unknown; resetKey: string };

export class ScreenBoundary extends Component<Props, State> {
  state: State = { error: null, resetKey: this.props.resetKey };

  static getDerivedStateFromError(error: unknown): Partial<State> {
    return { error: error ?? new Error("This screen failed to render.") };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (props.resetKey !== state.resetKey) return { error: null, resetKey: props.resetKey };
    return null;
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error("A screen failed to render", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="card">
          <h2>
            <Trans comment="Heading shown when a screen fails to draw; the menu still works">This screen could not be shown</Trans>
          </h2>
          <Problem error={this.state.error} />
          <p className="muted small">
            <Trans>The rest of the app still works. Pick another page from the menu.</Trans>
          </p>
        </div>
      );
    }
    return this.props.children;
  }
}
