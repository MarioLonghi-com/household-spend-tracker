/**
 * How import works: every path a statement can take, in plain words (#73).
 *
 * Information only -- no toggles, no actions. It sits under Admin for every
 * member, not just the owner, because members meet this behaviour on the
 * Import screen and nothing behind the page is private.
 *
 * `tests/test_import_guide.py` reads this file and fails when an import
 * outcome, a state a bank uses to say a row did not happen, a file format or
 * an identifier kind exists in the code and is not explained here. Change the
 * importer, change this page in the same commit.
 *
 * Sources: `statements/sniffing.py`, `statements/parsing.py`,
 * `app/services/importing.py`, `app/services/identifiers.py`,
 * `app/services/transfers.py`, and for the One-time Import (#183)
 * `app/services/one_time_import/`.
 */

import type { ReactNode } from "react";
import { NEW_ISSUE_URL } from "./OneTimeImport";

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="card">
      <h3 style={{ marginTop: 0 }}>{title}</h3>
      {children}
    </section>
  );
}

/** The outcome codes and the words the Import screen shows for them. */
const OUTCOMES: { code: string; label: string; meaning: string }[] = [
  {
    code: "created",
    label: "New",
    meaning:
      "Becomes a new transaction when you commit. If the line is one side of a transfer the importer is sure of, it is linked to the other side as well.",
  },
  {
    code: "matched_existing",
    label: "Already have it",
    meaning:
      "You typed this one in yourself, or a One-time Import brought it in: same amount, dated up to 4 days away — or, for a row YNAB had imported from the bank, that very bank line, whatever date it now has. Your row is kept and marked as seen by the bank instead of a second one being added. You can reject the match on the preview.",
  },
  {
    code: "duplicate_skipped",
    label: "Already imported",
    meaning:
      "This line is already in the account, from this file or an earlier statement that overlaps it. Nothing is added.",
  },
  {
    code: "needs_review",
    label: "Needs a look",
    meaning:
      "The row it matched changed between the preview and the commit, or was already matched to another statement line. It was left alone and not imported.",
  },
  {
    code: "rejected",
    label: "Could not read",
    meaning:
      "The line has no date or amount that could be read, its amount, fee or balance is too large to record as money, it says it is in a currency other than the account's, or the bank says it did not happen or has not settled yet (see below). One bad line costs one line, never the file.",
  },
  {
    code: "skipped",
    label: "Not for this account",
    meaning:
      "Read fine and deliberately not imported: it belongs to another account in a file that holds several (another product, or another currency), or it moves no money at all.",
  },
];

export function ImportGuide() {
  return (
    <>
      <div className="card">
        <h2>How import works</h2>
        <p className="muted">
          What happens to a statement from the moment you choose the file to the moment its rows
          are in the register, and every way a line can go. This page only explains; the Import
          screen is where it happens.
        </p>
      </div>

      <Section title="1. Getting the file in">
        <ul>
          <li>
            <strong>Upload a file, or paste the rows</strong> if your bank only lets you copy them
            from a web page. Both go through the same path.
          </li>
          <li>
            <strong>Formats read:</strong> CSV and other delimited text (comma, semicolon, tab or
            bar); OFX, both the old SGML shape (1.x) and XML (2.x); old Excel <code>.xls</code>;
            and PDF statements whose layout the app knows.
          </li>
          <li>
            <strong>Not read:</strong> <code>.xlsx</code> (save it as CSV or <code>.xls</code>),
            and camt.053 and MT940. Those are understood, but no real sample file has been seen
            here, and a format built from its specification alone is one that has never been
            tested against a real file.
          </li>
          <li>
            <strong>A file far larger than a statement is refused</strong>, with a sentence
            saying which limit it went over: a PDF of more than 50 pages, or one that unpacks to
            far more text and drawing than a statement holds; an <code>.xls</code> whose first
            sheet runs past a million cells; a single cell longer than 128 KB. Only the first sheet
            of an <code>.xls</code> is read.
          </li>
          <li>
            <strong>One account per import.</strong> You choose it. If the file says which account
            it is for — the account number an OFX file states, an identifier in the file's name,
            or an IBAN in its opening lines — the account is chosen for you and the screen says
            why. If the file names a different account from the one you picked, you are told.
            If it names none, and its file name carries one code that could be the account's tag,
            the screen offers to keep that code as the tag of the account you pick — only if you
            say <em>Add</em>.
          </li>
          <li>
            <strong>The same file twice is refused</strong> while the first is waiting or
            imported, with a way to open the waiting one. Once an import is undone, its file can
            go in again.
          </li>
          <li>
            <strong>A savings statement dropped on the wrong pocket is refused</strong> when every
            row names the other account's pocket.
          </li>
          <li>
            <strong>Agents</strong> holding a key can stage rows through the same checks. History
            shows the person whose key it was, and the agent that used it.
          </li>
        </ul>
      </Section>

      <Section title="2. How the file is read">
        <p className="small muted">
          Nothing is typed in. Every guess below is shown in the preview's "read as" block, so a
          wrong one is visible before anything is written.
        </p>
        <ul>
          <li>
            <strong>Where the table starts.</strong> A title block above it (account number,
            holder, balance) is stepped over. The encoding is worked out, so accented names, a
            Windows export's € and curly quotes, and a UTF-16 "Unicode text" file all arrive
            intact. A UTF-16 file without its byte-order mark is refused with a request to save it
            again as UTF-8, rather than guessed at.
          </li>
          <li>
            <strong>The date.</strong> Day-first or month-first is decided from the whole file, and
            you are warned when nothing in it settles the question. Dates written with a time keep
            only the date. When a file has both a started and a <em>completed</em> date, the
            completed one is used: it is when the bank booked it, the date the running balance
            follows, and the date the other side of a transfer carries.
          </li>
          <li>
            <strong>The amount.</strong> Either one signed column, or separate money-in and
            money-out columns. Currency symbols, thousands separators and parentheses for negatives
            are all read, and so is a minus written at the end (<code>12,50-</code>) or as a
            typographic minus or dash, and a debit or credit written after the figure
            (<code>12.50 DR</code> is money out, <code>12.50 CR</code> money in). An amount signed
            twice (<code>-12.50 DR</code>), or one that is not a number at all,
            is refused with the reason rather than read as zero. Whether a comma or a point is the
            decimal one is worked out from the amounts and the balance. When every one of them reads
            either way (<code>1.500</code> is fifteen hundred in Spain and one and a half in
            Britain), the account's country decides, or a point when it has none, and you are
            warned that it was assumed. If every amount in the file is positive and nothing says which way they
            go, you are warned: importing it as it is would read a month of spending as income.
          </li>
          <li>
            <strong>Never the amount:</strong> a balance, a rate (AER, NIR) or a date column, even
            when its name looks like one.
          </li>
          <li>
            <strong>A fee column</strong> becomes a row of its own under <em>Bank fees</em>, next
            to the payment it was charged on. The payment itself is not changed.
          </li>
          <li>
            <strong>A running balance</strong> is read and checked (see step 3). It is never taken
            as the amount.
          </li>
          <li>
            <strong>Everything the bank wrote is kept</strong> with each line, under the bank's own
            column names, so a transaction can be explained after the file is gone.
          </li>
        </ul>
      </Section>

      <Section title="3. Checks on the file as a whole">
        <ul>
          <li>
            <strong>A file holding several accounts.</strong> Some banks put more than one account
            in one file and tell them apart with a Product column. Revolut's account statement holds
            the current account (<code>Current</code>) and every savings pocket (
            <code>Deposit</code>). Each account takes one product: a current account takes{" "}
            <code>Current</code> unless you set its <em>statement product</em> on the Accounts
            screen. The other rows are listed as <em>Not for this account</em>. If an account
            has not said which product it takes and no safe guess exists, the file is refused
            with instructions.
          </li>
          <li>
            <strong>A file in another currency.</strong> A Currency column (or Moneda, Divisa,
            CCY) and an OFX file's own currency are checked against the account's. Every amount is
            recorded in the account's currency, so a row in another one is never imported. In a
            table that also holds rows in the account's currency it is listed as{" "}
            <em>Not for this account</em>; otherwise, and always in an OFX file (which is one
            account), as <em>Could not read</em>, with the bank's rate if it gave one. Nothing is
            converted. Either way the reason names both currencies. A file whose every row says it
            is in another currency is refused. An agent's row that names its currency is held to
            the same check, and one in another currency is <em>Could not read</em>. A column saying what a purchase cost before it was
            converted (Original, Moneda origen, Local, Transaction currency) does not count: that
            amount is already in the account's money. If two columns could each be the currency
            the amounts are in and neither is named for the account, the bill or the settlement,
            neither is used and the preview says so.
          </li>
          <li>
            <strong>Does the running balance add up?</strong> Each row's balance should be the one
            before it plus what the row moved. If it breaks, the preview says where: a row is
            missing, or rows from another account are mixed in. If a figure in the file is too
            large to record as money, that line is rejected and the preview says the balance was
            not checked.
          </li>
          <li>
            <strong>Does it carry on from the ledger?</strong> The balance before the file's
            first row should match what the register holds for the day before. If it doesn't, the
            preview gives the difference: a statement between your last import and this one is
            probably missing.
          </li>
          <li>
            <strong>A payee rule that claims most of a file</strong>, or one too short to be safe,
            is pointed out on the lines it claimed.
          </li>
        </ul>
      </Section>

      <Section title="4. What happens to each line">
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>On the preview</th>
                <th>Code</th>
                <th>What it means</th>
              </tr>
            </thead>
            <tbody>
              {OUTCOMES.map((one) => (
                <tr key={one.code}>
                  <td>
                    <span className={`pill ${one.code}`}>{one.label}</span>
                  </td>
                  <td className="mono small">{one.code}</td>
                  <td className="small">{one.meaning}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <h4>Rows the bank says did not happen, or have not happened yet</h4>
        <p className="small">
          A state column saying <code>REVERTED</code>, <code>DECLINED</code>, <code>FAILED</code>,{" "}
          <code>CANCELLED</code> (or <code>CANCELED</code>), <code>REJECTED</code> or{" "}
          <code>ANULADO</code> means no money moved, and the line is refused with the bank's own
          word as the reason. <code>PENDING</code>, <code>PENDIENTE</code> or <code>HOLD</code>{" "}
          means it has not settled: it is refused this time and imports from the next statement
          once it has settled, so a tip or an exchange rate added on settlement cannot create a
          second copy.
        </p>
        <h4>Lines that move no money</h4>
        <p className="small">
          A <code>0.00</code> line, such as a day's interest that rounded to nothing or a
          "Closing transaction", is shown and not imported.
        </p>
        <h4>How "already imported" is decided</h4>
        <p className="small">
          By the bank's own transaction id when the file has one (OFX <code>FITID</code>). Otherwise
          by the amount, the date and the running balance after the line, which stays the same
          whatever dates your download covered. Without a balance, it is the amount, the date and
          the line's position among identical ones that day. Statements that overlap therefore
          import only what is new. Lines imported by an older version of the app are still
          recognised.
        </p>
        <p className="small">
          A line counts as already imported only while its row is in the register. Delete the row
          and the line is new again: the next statement that carries it, or the same file sent
          again with <em>Import it anyway</em>, brings it back, known by the same identity — the
          statement is the record. Undo in History is the other way back, and it also restores
          anything you had changed on the row.
        </p>
        <h4>Payee and category</h4>
        <p className="small">
          Your payee naming rules decide the payee, and the payee's categorisation decides the
          category. Names and rules are compared ignoring capitals, accents, the kind of dash and
          spaces you cannot see, so <em>Café Sol</em> and <em>CAFE SOL</em> are one payee; a
          regular-expression rule is tried against the bank's own words as well as that plainer
          form. A category you choose on the preview wins over both. When neither says
          anything, the bank's fixed wording decides for a few kinds of line: interest goes to
          the <em>Interest</em> payee and <em>Interest income</em>, Revolut's investment account
          and robo portfolio to <em>Investments</em>, and fees to <em>Bank fees</em>. A category
          the importer needs and you don't have yet is created when you commit, and removed again
          if the import is undone. What the bank wrote is never overwritten.
        </p>
      </Section>

      <Section title="5. Transfers between your own accounts">
        <p className="small">
          Money moving between two of your accounts appears in both banks' statements, often
          imported days apart. Linked as one transfer, the two rows stay out of Income v Expense.
        </p>
        <ul className="small">
          <li>
            <strong>A candidate pair:</strong> two rows on different accounts, same currency,
            exactly opposite amounts, dated at most 5 days apart.
          </li>
          <li>
            <strong>A row with a category is not a transfer.</strong> It is never paired, never
            linked on commit and never waiting — and categorising happens before matching, so a
            row whose payee rule or history gives it a category is left alone. Clear the category
            and it is matched again.
          </li>
          <li>
            <strong>Linked on commit without asking</strong> when one row names the other account
            (its number, IBAN, card or a name the bank uses for it — see identifiers below), or
            when these two accounts have had transfers linked before — by a row that named the
            other side, or by you; a link made on that history alone does not count — <em>and</em>{" "}
            no other row could be its pair (or this one is strictly the closest in date, or — when
            date cannot tell two of them apart — its two descriptions share strictly more words
            than any rival's, not counting words like <em>TO</em>, <em>FROM</em> or your account
            numbers).
          </li>
          <li>
            <strong>Suggested</strong> when only the amounts and dates match, or when a row could
            pair with more than one. The preview says so, and the Transfers screen is where you
            confirm it.
          </li>
          <li>
            <strong>Never linked without asking, however well it matches:</strong> money out of a
            credit card (a purchase, and its refund or cashback is not a transfer), and a row whose
            payee has a category rule or whose words name someone who has paid you before — an
            employer's expense refund, say. These are suggested instead.
          </li>
          <li>
            <strong>A pair you have said is not a transfer</strong> — by unlinking it, or with{" "}
            <em>Not a transfer</em> on the Transfers screen — is never matched again. Undoing that
            in History, or linking the pair yourself, puts it back.
          </li>
          <li>
            <strong>Waiting for the other statement</strong> when a row names one of your accounts
            or a household member and its other side has not been imported yet. It is linked when
            that statement arrives.
          </li>
          <li>
            <strong>Two currencies</strong> are never matched automatically, because the amounts
            differ by a rate nobody printed. Select both rows in the register and choose{" "}
            <em>Link as transfer</em>; the rate is worked out and kept.
          </li>
        </ul>
      </Section>

      <Section title="6. Identifiers: what banks call your accounts">
        <p className="small">
          Set on the Accounts screen. They are how a file finds its account and how a transfer
          finds its other side.
        </p>
        <ul className="small">
          <li>
            <code>iban</code>, <code>number</code>, <code>card</code> — matched with spaces
            ignored. Short numbers (under six characters) only match as a whole word.
          </li>
          <li>
            <code>alias</code> — a name the bank uses for the account, such as a savings pocket's
            name. Matched as whole words.
          </li>
          <li>
            <code>file_tag</code> — the code a bank puts in its download's file name. Only used to
            recognise files.
          </li>
          <li>
            <code>holder</code> — how a bank writes a household member's name. It belongs to no
            account: it means "this is our own money moving". Banks write one person's name many
            ways, so a name of two words or more matches in any order, with initials for some of
            its words, and with the last word of the text cut short (four letters or more). A
            name of three words or more also matches when two of them stand together and one is
            four letters or longer. A one-word name only matches as that whole word. A name only
            ever makes a transfer <em>suggested</em>, never linked on its own; the Accounts screen
            shows what each name matches.
          </li>
        </ul>
        <p className="small">
          <strong>Suggested identifiers.</strong> The Accounts screen also lists identifiers your
          register already uses and nobody has added: words on the other side of transfers you
          have linked, IBANs and card numbers that pass their check digits, <code>A/C</code> and
          sort-code numbers, quoted pocket names, an account's own name, and the code every
          statement file of an account carries. Each says how many rows mention it and how many
          transfer pairs adding it would link. None is added by itself: an identifier lets
          transfers link without asking, so each waits for <em>Add</em> or <em>Ignore</em>, and an
          ignored one is not offered again.
        </p>
      </Section>

      <Section title="7. Staged, then committed, then undoable">
        <ul className="small">
          <li>
            <strong>Nothing reaches the register until you commit.</strong> A read file is staged
            and waits in the queue on the Import screen, where it can be opened again or thrown
            away.
          </li>
          <li>
            <strong>Every time a staged import is opened it is checked again</strong> against the
            register as it is now. A duplicate whose original has since been undone becomes new
            again, and the preview says how many lines changed. Commit checks once more, so a tab
            left open cannot import a line twice.
          </li>
          <li>
            <strong>One import is one act in History</strong>, and undoing it removes every row it
            added, every transfer link it made and every category it created. Reconciled rows are
            never matched as "Already have it".
          </li>
        </ul>
      </Section>

      <Section title="8. One-time Import: bringing another app's history across">
        <p className="small">
          Everything above is about statements, one file per account, month after month. The
          One-time Import is for the day you move here from another budgeting app: it brings that
          app's whole history in once — transactions, accounts, categories and transfers — instead
          of statement by statement. It lives on the household's own page, in the{" "}
          <em>One-time Import</em> section, and the household's owner runs it. YNAB is the one
          workflow so far.
        </p>
        <ul className="small">
          <li>
            <strong>Two ways in from YNAB.</strong> Upload its export — the zip as YNAB gives it,
            or the <code>Register.csv</code> inside it; the <code>Plan.csv</code> is not needed
            and is ignored — or connect with a YNAB personal access token and pick the plan. The
            token is used for that one visit and never stored: it is not kept in the ledger, in
            History or in any log, and closing the import forgets it.
          </li>
          <li>
            <strong>You map, it suggests.</strong> Each YNAB account goes to an account you
            already have, to a new one (<em>Create new…</em>), or is skipped. Each YNAB category
            goes to one of yours, to a new one (<em>Create new…</em>), or arrives uncategorised.
            Suggestions come from the names, and an account is only suggested in the same currency.
          </li>
          <li>
            <strong>The bank&rsquo;s own words come too, through the API.</strong> For a row
            YNAB imported from a bank, its API still has what the bank wrote, and that is kept
            beside YNAB&rsquo;s payee name — the payee is the name you kept in YNAB. The report
            then points at <em>Payee Naming Rules</em>, which can suggest rules from that text
            before your first statement arrives, and says how many groups of bank strings now
            look like one payee each. The <code>Register.csv</code> has only the clean names, so
            a file import keeps no bank text, and its report says rules can be suggested after
            the first statement import.
          </li>
          <li>
            <strong>Cleared states are reset.</strong> YNAB&rsquo;s reconciled and cleared marks
            do not carry over: every row arrives uncleared, to be reconciled here against the
            bank. The import asks you to confirm that before it commits.
          </li>
          <li>
            <strong>Transfers are linked.</strong> When both sides of a YNAB transfer are
            imported they become one linked transfer here. When one side is skipped, out of the
            date range or missing, the other arrives as an ordinary row and the report says why.
          </li>
          <li>
            <strong>Duplicates are asked about.</strong> A row that looks like one already in an
            account you mapped to — same amount, dated up to 3 days apart — is shown before
            anything is written, and skipped unless you say to import it, one by one or all at
            once. A row an earlier one-time import already brought in is always skipped, so
            running it again only adds what is new. A file&rsquo;s row is known by what it says —
            account, date, amounts, payee, category and memo — not by its line, so a fresh export
            with rows added above still skips everything imported before.
          </li>
          <li>
            <strong>Statements afterwards find these rows.</strong> A statement imported later
            for the same months matches each line to the row the One-time Import brought in —
            same amount, dated up to 4 days away — and marks it as seen by the bank instead of
            adding it again. For a row YNAB had itself imported from the bank, the match is
            exact: it is that bank line, even if you moved its date in YNAB. A row you have
            reconciled here is left alone. Running the One-time Import again still skips a row a
            statement has matched.
          </li>
          <li>
            <strong>Partial, not all-or-none.</strong> A row that cannot be read — a bad date, an
            amount that does not fit the currency — is left out and the rest still go in. A
            preview runs the whole import first and throws it away. The report after the commit
            counts every row: imported, linked, skipped and why, failed and why; what did not go in
            downloads as a <code>.txt</code> file.
          </li>
          <li>
            <strong>One undo reverses it.</strong> The whole import is a single entry in History,
            <em> One-time Import · YNAB (CSV)</em> or <em>(API)</em>. Undoing it takes back every
            row, transfer link, account, category and payee it created, and like the import
            itself it is the owner&rsquo;s to do. Each row it brought in
            says so in the register&rsquo;s <em>Where did this come from?</em>, with the file or
            plan and the date.
          </li>
          <li>
            <strong>Another app?</strong> More workflows can be added to the One-time Import.
            Suggest one on{" "}
            <a href={NEW_ISSUE_URL} target="_blank" rel="noopener noreferrer">
              GitHub
            </a>
            .
          </li>
        </ul>
      </Section>
    </>
  );
}
