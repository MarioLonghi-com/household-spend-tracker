/**
 * Layout B, the "guide rail" (#458): a rail of the steps under their
 * chapters on the left, the step in the middle, "About this step" on the
 * right. Below 760 px of its own width the rail gives way to a progress bar
 * and the panel moves under the form -- a container query in
 * `styles/setup.css`, not a second component.
 */

import { type ReactNode, useEffect, useRef } from "react";
import { useProductName } from "../../lib/capabilities";
import type { WizardCopy } from "./copy";
import { type Chapter, CHAPTER_OF, CHAPTERS, type StepKey } from "./steps";

export function chapterName(c: WizardCopy, chapter: Chapter): string {
  return { start: c.c_start, security: c.c_sec, household: c.c_hh, end: c.c_end }[chapter];
}

export function stepName(c: WizardCopy, step: StepKey): string {
  return {
    welcome: c.s_welcome,
    overview: c.s_overview,
    token: c.s_token,
    you: c.s_you,
    touch: c.s_touch,
    auth: c.s_auth,
    recovery: c.s_recovery,
    household: c.s_household,
    region: c.s_region,
    categories: c.s_categories,
    account: c.s_account,
    finish: c.s_finish,
  }[step];
}

export function Frame({
  c,
  steps,
  at,
  furthest,
  done,
  onJump,
  title,
  lead,
  about,
  children,
  nav,
}: {
  c: WizardCopy;
  steps: readonly StepKey[];
  at: number;
  furthest: number;
  /** Setup has finished: every step is done, and none can be reopened. */
  done: boolean;
  onJump: (index: number) => void;
  title: string;
  lead: ReactNode;
  about: ReactNode;
  children: ReactNode;
  nav: ReactNode;
}) {
  const productName = useProductName();
  const heading = useRef<HTMLHeadingElement>(null);
  const step = steps[at];
  // Each step change moves focus to its title, so a screen reader starts
  // there. No ring on it (`styles/setup.css`): Tab never lands on it (#470).
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    heading.current?.focus();
  }, [at, done]);

  const progress = `${c.step({ n: at + 1, m: steps.length })}, ${chapterName(c, CHAPTER_OF[step])}, ${stepName(c, step)}`;

  return (
    <div className="wizard-shell">
      <div className="wizard">
        <nav className="wizard-rail" aria-label={c.nav_label}>
          <div className="wizard-product">{productName}</div>
          {CHAPTERS.map((chapter) => {
            const mine = steps.map((one, index) => ({ one, index })).filter(({ one }) => CHAPTER_OF[one] === chapter);
            if (!mine.length) return null;
            return (
              <div key={chapter} className="wizard-chapter">
                <div className="wizard-chapter-name">{chapterName(c, chapter)}</div>
                <ol>
                  {mine.map(({ one, index }) => {
                    const state = done || index < at ? "done" : index === at ? "current" : index <= furthest ? "reached" : "ahead";
                    return (
                      <li key={one}>
                        <button
                          type="button"
                          className={`wizard-step ${state}`}
                          aria-current={index === at && !done ? "step" : undefined}
                          disabled={done || index > furthest || index === at}
                          onClick={() => onJump(index)}
                        >
                          <span className="wizard-dot" aria-hidden="true">
                            {state === "done" ? "✓" : index + 1}
                          </span>
                          <span>{stepName(c, one)}</span>
                        </button>
                      </li>
                    );
                  })}
                </ol>
              </div>
            );
          })}
          <p className="wizard-foot">{c.saved_note}</p>
        </nav>

        <div className="wizard-main">
          <div className="wizard-progress" aria-hidden="true">
            <div className="wizard-progress-text">
              <span>{c.step({ n: at + 1, m: steps.length })}</span>
              <span>
                {chapterName(c, CHAPTER_OF[step])} · {stepName(c, step)}
              </span>
            </div>
            <div className="wizard-bar">
              <span style={{ width: `${((at + 1) / steps.length) * 100}%` }} />
            </div>
          </div>
          <p className="sr-only" aria-live="polite">
            {done ? "" : progress}
          </p>

          <div className="wizard-body">
            <div className="wizard-form">
              <h1 ref={heading} tabIndex={-1}>
                {title}
              </h1>
              <p className="wizard-lead">{lead}</p>
              {children}
              <div className="wizard-nav">{nav}</div>
            </div>
            <aside className="wizard-about">
              <h2>{c.about}</h2>
              <div className="muted small">{about}</div>
            </aside>
          </div>
        </div>
      </div>
    </div>
  );
}
