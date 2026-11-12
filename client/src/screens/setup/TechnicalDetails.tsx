/**
 * "Technical details" on the last step (#458): where this installation keeps
 * its data, how it is backed up and upgraded, and which build it is. Closed
 * by default; nobody needs it to use the app. The facts come from the server
 * (`/setup/state` -> `install`), and a row with nothing known is left out
 * rather than guessed. Paths, commands and file names, in backticks in the
 * words, are drawn as code and never translated.
 */

import type { ReactNode } from "react";
import type { WizardCopy } from "./copy";
import type { Edition } from "./steps";

export interface Install {
  edition: string;
  data_dir: string | null;
  version: string | null;
  commit: string | null;
}

const PROJECT = "https://mariolonghi.com/projects";

/** Text with `code` spans, as elements. Nothing is parsed as markup. */
export function withCode(text: string): ReactNode[] {
  return text.split("`").map((part, index) => (index % 2 ? <code key={index}>{part}</code> : part));
}

export function TechnicalDetails({ c, edition, install }: { c: WizardCopy; edition: Edition; install: Install | null }) {
  if (!install) return null;
  const path = install.data_dir ?? "";
  const rows: [string, string | null][] =
    edition === "web"
      ? [
          [c.data, path ? c.webData({ path }) : null],
          [c.backup, c.webBackup],
          [c.upgrade, c.webUpgrade],
          [c.version, install.version && install.commit ? c.webVersion({ version: install.version, commit: install.commit }) : null],
        ]
      : edition === "mac"
        ? [
            [c.data, path ? c.macData({ path }) : null],
            [c.backup, c.macBackup],
            [c.upgrade, c.macUpgrade],
            // The build number is the shell's, and the page is not told it.
            [c.version, null],
          ]
        : [
            [c.data, path ? c.winData({ path }) : null],
            [c.backup, c.winBackup],
            [c.upgrade, c.winUpgrade],
            [c.version, install.version ? c.winVersion({ version: install.version }) : null],
          ];

  return (
    <details className="wizard-details">
      <summary>{c.title}</summary>
      <p className="muted small">{c.sub}</p>
      <dl>
        {rows
          .filter(([, text]) => text)
          .map(([label, text]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd className="small">{withCode(text!)}</dd>
            </div>
          ))}
        <div>
          <dt>{c.project}</dt>
          <dd className="small">
            {c.project_t}{" "}
            <a href={PROJECT} target="_blank" rel="noreferrer">
              {PROJECT}
            </a>
          </dd>
        </div>
      </dl>
    </details>
  );
}
