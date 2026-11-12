/**
 * Step 0b (#458): what the app is, as one picture with transactions at the
 * centre. Read-only. The whole diagram is also one sentence, in a visually
 * hidden caption, for a screen reader.
 */

import type { ReactNode } from "react";
import type { WizardCopy } from "./copy";

function Box({
  emoji,
  name,
  text,
  className = "",
  children,
}: {
  emoji: string;
  name: string;
  text?: string;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <div className={`ov-box ${className}`}>
      <div className="ov-name">
        <span aria-hidden="true">{emoji} </span>
        {name}
      </div>
      {text ? <div className="ov-text">{text}</div> : null}
      {children}
    </div>
  );
}

function Arrow() {
  return (
    <div className="ov-arrow" aria-hidden="true">
      ↓
    </div>
  );
}

export function Overview({ c }: { c: WizardCopy }) {
  return (
    <figure className="overview">
      <figcaption className="sr-only">{c.ov_sum}</figcaption>
      <div aria-hidden="true">
        <div className="ov-band-name">{c.ov_in}</div>
        <div className="ov-pair">
          <Box emoji="✍️" name={c.ov_manual} text={c.ov_manual_d} />
          <Box emoji="🏦" name={c.ov_import} text={c.ov_import_d} />
        </div>
        <Arrow />
        <Box emoji="🏠" name={c.ov_hh} className="ov-household">
          <Box emoji="💳" name={c.ov_acc} text={c.ov_acc_d} className="ov-accounts">
            <Box emoji="🔁" name={c.ov_tx} text={c.ov_tx_d} className="ov-transactions">
              <Box emoji="🧾" name={c.ov_rc} text={c.ov_rc_d} className="ov-receipts" />
            </Box>
          </Box>
        </Box>
        <Arrow />
        <div className="ov-band-name">{c.ov_out}</div>
        <div className="ov-pair">
          <Box emoji="📊" name={c.ov_ie} text={c.ov_ie_d} className="ov-report" />
          <Box emoji="📈" name={c.ov_nw} text={c.ov_nw_d} className="ov-report" />
        </div>
        <hr className="rule" />
        <div className="ov-footer">
          <div className="ov-name">
            <span>🌍 </span>
            {c.ov_fx}
          </div>
          <div className="ov-text">{c.ov_fx_d}</div>
        </div>
      </div>
    </figure>
  );
}
