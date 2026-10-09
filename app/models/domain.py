"""The ledger itself: households, accounts, payees, rules and transactions.

Iteration 1 has no budget, so there are no categories, no tags and therefore no
splits -- a split divides a purchase across categories, and without categories
its children would differ from the parent in no respect this schema records.

Every relationship below that owns its children declares
``cascade="all, delete-orphan"`` and does **not** set ``passive_deletes``. That
is deliberate: a database-level cascade deletes rows the ORM never sees, so the
audit log would record deleting an account and silently lose the hundreds of
transactions that went with it -- and an undo would then "succeed" and restore
an account with no history.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, EnumStr, Timestamped, UUIDPrimaryKey, utcnow
from .enums import (
    AccountType,
    BlobRole,
    Categorisation,
    ClearedState,
    IdentifierKind,
    LinkSource,
    MatchType,
    ReimbursementState,
    RuleAction,
    SuggestionStatus,
    SystemPayee,
)


class Household(Base, UUIDPrimaryKey, Timestamped):
    """What the previous build called a ledger. Not mono-currency: the currency
    here is only the one reports will eventually roll up into."""

    __tablename__ = "households"
    __audit__ = True

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    date_format: Mapped[str] = mapped_column(String(32), default="YYYY-MM-DD", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    #: Which colour scheme this household wears. Two households in one browser
    #: look identical otherwise, and that is how a statement lands in the wrong
    #: ledger. Only the key is stored; `app/theming.py` holds the colours, so a
    #: palette can be retouched between releases without a data migration.
    theme: Mapped[str] = mapped_column(String(32), default="moss", nullable=False)
    #: An accent hue of this household's own, overriding the palette's. Stored
    #: as picked; what each scheme actually shows is derived, because one value
    #: cannot sit on both a white page and a near-black one.
    accent: Mapped[str | None] = mapped_column(String(7))
    #: Keep the uploaded bytes of a receipt as well as the two AVIF
    #: derivatives. **Off**, which is what every install has done since
    #: receipts existed, and the measured reason: the pair of derivatives is
    #: 33 KB against the original's 2.3 MB, a factor of seventy. Issue #62.
    #:
    #: A household setting rather than the environment variable it used to be
    #: only. These are *their* documents and it is their judgement whether the
    #: disk is worth it, and an env var cannot be seen by them, changed by
    #: them, or audited. `SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL` still works and
    #: is now a **floor**: an operator who turns it on turns it on everywhere,
    #: because a self-hoster who has decided should not have to visit every
    #: household.
    receipts_keep_original: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    members: Mapped[list[HouseholdMember]] = relationship(
        "HouseholdMember", back_populates="household", cascade="all, delete-orphan"
    )
    accounts: Mapped[list[Account]] = relationship(
        "Account", back_populates="household", cascade="all, delete-orphan"
    )
    category_groups: Mapped[list[CategoryGroup]] = relationship(
        "CategoryGroup",
        back_populates="household",
        cascade="all, delete-orphan",
        order_by="CategoryGroup.sort_order, CategoryGroup.name",
    )
    categories: Mapped[list[Category]] = relationship(
        "Category", back_populates="household", cascade="all, delete-orphan"
    )
    payees: Mapped[list[Payee]] = relationship(
        "Payee", back_populates="household", cascade="all, delete-orphan"
    )
    #: Holder names belong to no account, so the household is what takes them
    #: -- through the ORM, so each one it takes is logged.
    identifiers: Mapped[list[AccountIdentifier]] = relationship(
        "AccountIdentifier", back_populates="household", cascade="all, delete-orphan"
    )
    payee_rules: Mapped[list[PayeeRule]] = relationship(
        "PayeeRule", back_populates="household", cascade="all, delete-orphan"
    )
    transfer_rejections: Mapped[list[TransferRejection]] = relationship(
        "TransferRejection", back_populates="household", cascade="all, delete-orphan"
    )
    ignored_identifier_suggestions: Mapped[list[IgnoredIdentifierSuggestion]] = relationship(
        "IgnoredIdentifierSuggestion", back_populates="household", cascade="all, delete-orphan"
    )
    translation_suggestions: Mapped[list[TranslationSuggestion]] = relationship(
        "TranslationSuggestion", back_populates="household", cascade="all, delete-orphan"
    )
    #: Deleted with the household, through the ORM. `receipts.household_id` is
    #: RESTRICT at the database level precisely so this is the only path: the
    #: hook walks it and writes a delete row for each.
    receipts: Mapped[list[Receipt]] = relationship(
        "Receipt", back_populates="household", cascade="all, delete-orphan"
    )
    #: The same arrangement, and for the same reason: `agent_keys.household_id`
    #: is RESTRICT so this is the only way a key can go, and the hook walks it.
    agent_keys: Mapped[list[AgentKey]] = relationship(  # noqa: F821
        "AgentKey", back_populates="household", cascade="all, delete-orphan"
    )


class HouseholdMember(Base, UUIDPrimaryKey):
    """Binary membership: you are in a household or you are not, and if you are
    you can do everything inside it.

    A surrogate id rather than a composite key, because every change row needs a
    single ``row_id`` -- a composite key would have to be JSON-encoded there and
    every audit query would pay for it.
    """

    __tablename__ = "household_members"
    __audit__ = True

    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    added_by_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"))
    added_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    household: Mapped[Household] = relationship("Household", back_populates="members")

    __table_args__ = (UniqueConstraint("household_id", "user_id", name="uq_household_members_pair"),)


class Account(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "accounts"
    __audit__ = True

    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    type: Mapped[AccountType] = mapped_column(EnumStr(AccountType, 24), nullable=False)
    #: Currency lives here, not on the household. The ledger never converts.
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    institution: Mapped[str | None] = mapped_column(String(120))
    #: ISO 3166-1 alpha-2, which is also what a flag emoji is made of -- so the
    #: code is the flag and there is no picture to keep in step with it.
    #: Nullable: most accounts will never say, and "not stated" is a real
    #: answer rather than a missing one.
    country: Mapped[str | None] = mapped_column(String(2))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    household: Mapped[Household] = relationship("Household", back_populates="accounts")
    transactions: Mapped[list[Transaction]] = relationship(
        "Transaction",
        back_populates="account",
        foreign_keys="Transaction.account_id",
        cascade="all, delete-orphan",
    )
    reconciliations: Mapped[list[Reconciliation]] = relationship(
        "Reconciliation",
        back_populates="account",
        cascade="all, delete-orphan",
        order_by="Reconciliation.statement_date.desc()",
    )
    #: Through the relationship rather than the database's own cascade, so an
    #: account's deletion logs each identifier it takes with it.
    identifiers: Mapped[list[AccountIdentifier]] = relationship(
        "AccountIdentifier", back_populates="account", cascade="all, delete-orphan"
    )
    #: In a statement that holds several accounts, the value of its product
    #: column that means *this* one: Revolut's account statement carries
    #: checking as `Current` and both savings pockets as `Deposit` (issue #68).
    #: Null for every account whose statements hold only itself.
    statement_product: Mapped[str | None] = mapped_column(String(40))

    __table_args__ = (Index("ix_accounts_household_name", "household_id", "name"),)


class AccountIdentifier(Base, UUIDPrimaryKey, Timestamped):
    """One name a bank uses for an account, or for a household member.

    A table rather than columns on `accounts`: one account has an IBAN, a card
    number, a pocket name and a file tag at once, and a holder's name belongs
    to no account at all. Before this, the only place an IBAN could live was
    the account's display name.
    """

    __tablename__ = "account_identifiers"
    __audit__ = True

    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Null for `holder`, which names a person rather than an account.
    account_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[IdentifierKind] = mapped_column(EnumStr(IdentifierKind, 16), nullable=False)
    #: As typed, so it reads back the way the person wrote it.
    value: Mapped[str] = mapped_column(String(120), nullable=False)
    #: What matching compares: see `services/identifiers.normalise`. Derived,
    #: but stored, because it is what the uniqueness is about -- "ES91 2100"
    #: and "es912100" are one identifier and must not name two accounts.
    normalised: Mapped[str] = mapped_column(String(120), nullable=False)

    account: Mapped[Account | None] = relationship("Account", back_populates="identifiers")
    household: Mapped[Household] = relationship("Household", back_populates="identifiers")

    __table_args__ = (
        UniqueConstraint("household_id", "normalised", name="uq_account_identifiers_value"),
    )


class Reconciliation(Base, UUIDPrimaryKey, Timestamped):
    """One act of proving an account against a statement.

    Stored because it is a deliberate act and because it keeps something that
    cannot be derived: **what the bank said the balance was**. The ledger's own
    figure on that date is arithmetic and can always be recomputed; the
    statement's closing balance is a claim somebody else made, and once the PDF
    is gone this row is the only record that the two were ever compared.

    Everything else about a reconciliation *is* derivable and is not stored: how
    many rows were locked, and which, are in the batch this points at.
    """

    __tablename__ = "reconciliations"
    __audit__ = True

    account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The statement's closing date. Rows dated after it are not part of it.
    statement_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: What the bank said, in the account's currency, minor units.
    statement_balance: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The batch that locked the rows, so "undo this reconciliation" is the
    #: same one button as undoing anything else.
    batch_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("batches.id", ondelete="SET NULL")
    )

    account: Mapped[Account] = relationship("Account", back_populates="reconciliations")

    __table_args__ = (
        Index("ix_reconciliations_account_date", "account_id", "statement_date"),
    )


class CategoryGroup(Base, UUIDPrimaryKey, Timestamped):
    """A heading the categories sit under.

    Two levels rather than one because a flat list of thirty is unusable at the
    point it matters -- picking one while entering a row. Two levels is also as
    deep as it goes: a third would be a tree, and a tree needs a reason nobody
    has given.
    """

    __tablename__ = "category_groups"
    __audit__ = True

    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    household: Mapped[Household] = relationship("Household", back_populates="category_groups")
    categories: Mapped[list[Category]] = relationship(
        "Category",
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="Category.sort_order, Category.name",
    )

    __table_args__ = (
        UniqueConstraint("household_id", "name", name="uq_category_groups_household_name"),
    )


class Category(Base, UUIDPrimaryKey, Timestamped):
    """What a transaction was for.

    Classification only. There is no budget in this app and there is not going
    to be one, so a category carries no assigned amount, no target and no
    rollover -- it answers "what was this?" and nothing else.
    """

    __tablename__ = "categories"
    __audit__ = True

    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("category_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Hidden from the pickers but still attached to everything it ever
    #: categorised. Deleting a used category would silently rewrite history;
    #: archiving keeps the past readable and keeps the list short.
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    household: Mapped[Household] = relationship("Household", back_populates="categories")
    group: Mapped[CategoryGroup] = relationship("CategoryGroup", back_populates="categories")

    __table_args__ = (
        UniqueConstraint("household_id", "name", name="uq_categories_household_name"),
        Index("ix_categories_household_group", "household_id", "group_id"),
    )

    @property
    def full_name(self) -> str:
        """"Quality of Life: Subscriptions" -- how it reads outside the picker."""
        return f"{self.group.name}: {self.name}"


class Payee(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "payees"
    __audit__ = True

    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Folded for lookup, so MERCADONA and Mercadona are one payee rather than
    #: two. The previous build matched exactly and fragmented the payee list.
    name_folded: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    #: Set for the auto-managed "Transfer : <Account>" payees.
    #:
    #: RESTRICT, not CASCADE: a database-level cascade deletes rows the ORM
    #: never sees, so the audit log would miss them and an undo would "succeed"
    #: having destroyed them. Removing an account has to deal with what points
    #: at it, out loud.
    transfer_account_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    #: Set when the app made this payee rather than a person, and what it made
    #: it *for*. Null is the ordinary case: somebody you actually pay.
    #:
    #: This is what lets a flow report exclude opening balances and transfers
    #: without string-matching a name. Before it existed the only mark on an
    #: opening balance was the payee name "Opening balance", so renaming that
    #: payee turned an account's starting balance into a month of income. See
    #: :class:`SystemPayee`.
    system: Mapped[SystemPayee | None] = mapped_column(EnumStr(SystemPayee, 16))
    #: How a new transaction for this payee gets its category.
    categorisation: Mapped[Categorisation] = mapped_column(
        EnumStr(Categorisation, 8), default=Categorisation.history, nullable=False
    )
    #: The answer when `categorisation` is `fixed`. SET NULL rather than
    #: RESTRICT: archiving is the normal way to retire a category, and a payee
    #: pointing at a deleted one should lose its default, not block the delete.
    default_category_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("categories.id", ondelete="SET NULL")
    )

    household: Mapped[Household] = relationship("Household", back_populates="payees")
    default_category: Mapped[Category | None] = relationship("Category")
    #: Not a convenience: this is what tells the unit of work that a payee
    #: must go before the account it names. `transfer_account_id` is RESTRICT,
    #: which SQLite checks at the statement even with foreign keys deferred,
    #: and without a relationship SQLAlchemy has no edge between the two
    #: tables -- so a flush deleting both (undoing an import that created an
    #: account and linked a transfer into it) ran them in whichever order the
    #: sort happened to produce, and failed about half the time.
    transfer_account: Mapped[Account | None] = relationship(
        "Account", foreign_keys=[transfer_account_id]
    )
    #: Declared so deleting a payee takes its rules through the ORM, where the
    #: audit hook can see them, rather than through a database cascade.
    rules: Mapped[list[PayeeRule]] = relationship(
        "PayeeRule", back_populates="payee", cascade="all, delete-orphan"
    )
    #: Same reason, one table over. `transactions.payee_id` is ON DELETE SET
    #: NULL, and SQLite applies that itself -- so deleting a payee (undoing the
    #: batch that created one, say) used to clear the payee off every
    #: transaction that referenced it with no change row written and no way
    #: back. Loaded here, the nulling happens through the ORM as an ordinary
    #: audited update, and the undo of that undo puts the links back.
    transactions: Mapped[list[Transaction]] = relationship(
        "Transaction", back_populates="payee"
    )

    __table_args__ = (
        UniqueConstraint("household_id", "name_folded", name="uq_payees_household_folded"),
    )


class PayeeRule(Base, UUIDPrimaryKey, Timestamped):
    """A rule applied as statement rows are read. Two kinds, see `action`.

    A `map` rule turns a raw bank string into the payee you actually recognise.
    A `rewrite` rule turns it into a *different string*, which the map rules
    then run against -- which is the only shape that fits a payment rail, where
    one shared prefix hides a different merchant on every line. Issue #58.
    """

    __tablename__ = "payee_rules"
    __audit__ = True

    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    match_type: Mapped[MatchType] = mapped_column(EnumStr(MatchType, 16), nullable=False)
    #: What to do with a row this rule claims. Defaulted in the migration as
    #: well as here, so every rule written before the column existed is a `map`
    #: rule and behaves exactly as it did.
    action: Mapped[RuleAction] = mapped_column(
        EnumStr(RuleAction, 16), default=RuleAction.map, nullable=False
    )
    pattern: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Nullable **because a rewrite rule has no payee to point at**. There is
    #: no one payee behind `PAGO MOVIL`; that is the entire defect this column
    #: exists to fix. A `map` rule without one is refused in `create_rule`,
    #: which is where the constraint can say a sentence -- the database cannot
    #: express "required, but only for one value of another column" without a
    #: CHECK that SQLite would make a migration out of every time it moved.
    payee_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("payees.id", ondelete="CASCADE")
    )
    #: What a `rewrite` rule puts in place of what it matched. `None` means
    #: "put nothing there", which is the strip case and the common one. For a
    #: `regex` rule this is a template, so `\1` is the first group.
    replacement: Mapped[str | None] = mapped_column(String(300))
    #: Lower runs first.
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    household: Mapped[Household] = relationship("Household", back_populates="payee_rules")
    #: Loaded with the rule so a whole import can resolve payees without a
    #: query per row.
    payee: Mapped[Payee | None] = relationship("Payee")


class Transaction(Base, UUIDPrimaryKey, Timestamped):
    """The only table that records money.

    Every balance is a sum over these rows, which is why the register is the
    thing to protect. Still well under the previous build's twenty-six columns:
    no split *parent*, no approval flag, and -- because deletes are hard now --
    no tombstone. Reimbursement came back (2026-09-25) as two nullable columns
    rather than the previous build's tag, four-state machine and unenforced
    pointer.
    """

    __tablename__ = "transactions"
    __audit__ = True

    #: RESTRICT rather than CASCADE: a household reaches its transactions
    #: through its accounts, which the ORM walks and the audit log therefore
    #: sees. A second, database-level path would delete the same rows without
    #: the hook ever hearing about it.
    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: Signed minor units, in the account's own currency.
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    payee_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("payees.id", ondelete="SET NULL"))
    payee: Mapped[Payee | None] = relationship("Payee", back_populates="transactions")
    #: What this was for. Nullable on purpose: an uncategorised row is a real
    #: state, not a missing one -- an import creates hundreds of them and they
    #: are still perfectly good transactions.
    category_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("categories.id", ondelete="SET NULL"), index=True
    )
    category: Mapped[Category | None] = relationship("Category")
    #: The parts of one split share this. It is a grouping, not a parent: a
    #: split *replaces* the row it divides rather than hanging children off it.
    #:
    #: The previous build kept the original as a parent with `is_split`, which
    #: meant every balance, every report and every register query had to
    #: remember to exclude it or count the money twice. Here every row is a real
    #: transaction and no sum needs to know about splits at all. What the
    #: original was is in the audit log, where the batch that did it also holds
    #: the undo.
    split_id: Mapped[str | None] = mapped_column(String(32), index=True)
    memo: Mapped[str | None] = mapped_column(Text)
    cleared: Mapped[ClearedState] = mapped_column(
        EnumStr(ClearedState, 16), default=ClearedState.uncleared, nullable=False
    )

    # Transfers: a mirrored pair.
    #: The other side of a transfer. RESTRICT for the same reason as on Payee:
    #: this points at a row in a *different* account, and a silent cascade would
    #: destroy one leg of a transfer with nothing recorded anywhere.
    transfer_account_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    #: A real foreign key this time. The previous build left it unconstrained,
    #: reasoning that the pair is written in two steps -- but that only fails if
    #: the pointer is set before the counterpart exists. Insert both legs with
    #: it null, flush, then point them at each other. SET NULL because a mutual
    #: pair otherwise cannot have either leg deleted.
    transfer_transaction_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("transactions.id", ondelete="SET NULL"), index=True
    )
    #: For a cross-currency transfer: the rate between the legs, as a string so
    #: it stays exact and auditable.
    transfer_fx_rate: Mapped[str | None] = mapped_column(String(40))
    #: How this leg came to be linked to the other: see :class:`LinkSource`.
    #: Null on a row that is not a transfer leg. Both legs carry the same
    #: value, because the matcher reads whichever leg its query finds.
    link_source: Mapped[LinkSource | None] = mapped_column(EnumStr(LinkSource, 8))

    # Reimbursement: a work expense, and the payment that paid it back.
    #: The deliberate act of saying somebody else should pay this. NULL on
    #: nearly every row. Only ever set on money going out that is not a
    #: transfer leg -- see services/transactions.py::set_reimbursement.
    reimbursement: Mapped[ReimbursementState | None] = mapped_column(
        EnumStr(ReimbursementState, 16)
    )
    #: The payment that settled it. **Many expenses point at one payment** --
    #: that is how one transfer from work covers six hotel nights, and why the
    #: pointer lives on the expense rather than on the money coming in. The
    #: rows sharing one of these *are* the claim; there is no claim table
    #: because there is nothing left for it to hold.
    #:
    #: SET NULL rather than RESTRICT: deleting the payment must neither be
    #: blocked by what it settled nor destroy it. Those rows go back to
    #: outstanding, which is true again -- and the `reimburses` relationship
    #: below is what makes that an audited update rather than a silent one.
    reimbursed_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("transactions.id", ondelete="SET NULL"), index=True
    )

    # Import provenance.
    import_id: Mapped[str | None] = mapped_column(String(64), index=True)
    import_payee_original: Mapped[str | None] = mapped_column(String(300))
    import_source: Mapped[str | None] = mapped_column(String(60))
    #: Other keys the same bank line is known by, as a JSON list (#264). Not
    #: unique and not part of `uq_transactions_account_import`: they say what a
    #: row *was* or *would be* called, never what it is. Two kinds today:
    #:
    #: - ``ynab:<ref>``, kept when a statement line absorbs a One-time Import
    #:   row and its `import_id` becomes the statement's, so a repeat
    #:   One-time Import still finds its own row;
    #: - ``ST:<minor>:<date>:<nth>``, YNAB's own id for the bank line it
    #:   imported, translated to the statement key's shape, so the statement
    #:   holding that line matches this row exactly.
    #:
    #: Null on nearly every row. Reassigned, never mutated in place: a JSON
    #: column only notices a new value.
    import_alt_ids: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True))

    account: Mapped[Account] = relationship(
        "Account", back_populates="transactions", foreign_keys=[account_id]
    )
    #: The same ordering edge as `Payee.transfer_account`, for the same
    #: RESTRICT: a leg in an account that stays must be deleted before the
    #: other side's account is, and only a relationship makes the unit of work
    #: know that.
    transfer_account: Mapped[Account | None] = relationship(
        "Account", foreign_keys=[transfer_account_id]
    )
    #: Deliberately no delete cascade. Deleting a transaction drops its
    #: receipts to the inbox rather than destroying them, and the relationship
    #: is what makes that a *logged* update: `hook._nullify_referencing_children`
    #: only walks one-to-many relationships without a delete cascade, so
    #: without this the database's own SET NULL would detach the receipt with
    #: no change row -- and the undo would put the transaction back with no
    #: evidence on it.
    receipts: Mapped[list[Receipt]] = relationship(
        "Receipt", back_populates="transaction"
    )
    #: The payment this expense was repaid by, and the expenses a payment
    #: repaid. Neither is a convenience.
    #:
    #: `reimburses` is the one that matters, for the reason `receipts` gives
    #: above: without a one-to-many here, deleting a payment lets SQLite's own
    #: SET NULL detach every expense it settled with no change row, and the
    #: undo puts the payment back with nothing pointing at it.
    #:
    #: `foreign_keys=` is required on both, because `transactions` now has two
    #: foreign keys to itself and SQLAlchemy cannot choose. It raises at mapper
    #: configuration -- first use, not import -- so lint passes and the first
    #: request fails.
    reimbursed_by: Mapped[Transaction | None] = relationship(
        "Transaction",
        remote_side="Transaction.id",
        foreign_keys="Transaction.reimbursed_by_id",
        back_populates="reimburses",
    )
    reimburses: Mapped[list[Transaction]] = relationship(
        "Transaction",
        foreign_keys="Transaction.reimbursed_by_id",
        back_populates="reimbursed_by",
    )

    __table_args__ = (
        #: The whole defence against importing the same statement twice.
        UniqueConstraint("account_id", "import_id", name="uq_transactions_account_import"),
        Index("ix_transactions_household_date", "household_id", "date"),
        Index("ix_transactions_account_date", "account_id", "date"),
        #: The category history an import asks for once per payee, newest
        #: first -- a full scan of the ledger without it (issue #100).
        Index("ix_transactions_payee_date", "payee_id", "date"),
        #: The transfer matcher's "linked before" lanes, and the RESTRICT check
        #: every account delete runs.
        Index("ix_transactions_transfer_account_id", "transfer_account_id"),
        #: The Reimbursements report and the register's Work expenses filter,
        #: which always arrive scoped to one household. Composite because the
        #: column is NULL on nearly every row, and the planner will not choose
        #: a low-selectivity index on it alone.
        Index("ix_transactions_household_reimbursement", "household_id", "reimbursement"),
    )


class TransferRejection(Base, UUIDPrimaryKey, Timestamped):
    """Two rows a person has said are not one transfer (#131).

    Written when a link is undone with Unlink, and by "Not a transfer" on a
    pair the matcher offered. The matcher never offers the pair again, strong
    or suggested; without this, the next sweep put every wrong link a person
    had just removed straight back under "Sure of these".

    Audited, so it goes with the act: undoing the unlink in History restores
    the link and removes this row in the same step. Linking the pair by hand
    later removes it too -- the person has changed their mind.

    The two transaction ids are plain columns, not foreign keys. A deleted row
    comes back from the audit log with its old id, and the rejection should
    still hold for it then; a database cascade would have removed it without
    the log hearing.
    """

    __tablename__ = "transfer_rejections"
    __audit__ = True

    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The row money left.
    out_transaction_id: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The row money arrived in.
    in_transaction_id: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Who said so. Not the batch actor, for the same reason as a receipt's
    #: uploader: an undo of an undo re-inserts the row.
    rejected_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL")
    )

    household: Mapped[Household] = relationship("Household", back_populates="transfer_rejections")

    __table_args__ = (
        UniqueConstraint(
            "household_id",
            "out_transaction_id",
            "in_transaction_id",
            name="uq_transfer_rejections_pair",
        ),
    )


class IgnoredIdentifierSuggestion(Base, UUIDPrimaryKey, Timestamped):
    """An identifier the ledger suggested and a person said no to (#130).

    The Accounts screen offers identifiers it can read off the ledger -- a
    pocket's quoted name, an `A/C` number, a card's last four -- and a person
    adds each one or ignores it. An ignored one is never offered again, for
    any account: what is remembered is the value and its kind.

    Audited, so ignoring is undone in History like any other act. A value is
    stored as the suggestion showed it; the snapshot keeps only its last four
    (`scripts/db_view.py`), since an ignored number is still somebody's number.
    """

    __tablename__ = "ignored_identifier_suggestions"
    __audit__ = True

    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[IdentifierKind] = mapped_column(EnumStr(IdentifierKind, 16), nullable=False)
    value: Mapped[str] = mapped_column(String(120), nullable=False)
    #: As `services/identifiers.normalise` writes it: what "the same
    #: suggestion" means, however it was spaced.
    normalised: Mapped[str] = mapped_column(String(120), nullable=False)
    ignored_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL")
    )

    household: Mapped[Household] = relationship(
        "Household", back_populates="ignored_identifier_suggestions"
    )

    __table_args__ = (
        UniqueConstraint(
            "household_id", "kind", "normalised", name="uq_ignored_identifier_suggestions_value"
        ),
    )


class TranslationSuggestion(Base, UUIDPrimaryKey, Timestamped):
    """A better wording for one message of a draft language, from review mode (#272).

    An owner reviewing a draft language in the app picks a message and writes
    what it should say. The message is named the way the catalogs name it --
    its English source and, when it has one, its context -- because that is
    what `scripts/apply_translation_suggestions.py` finds it by in a `.po`
    file, and what a person reading the export can read.

    Audited, like every deliberate act: a suggestion and its withdrawal are in
    History and undone there. Swept by nothing -- it is the household's own
    words, kept until somebody deletes it.
    """

    __tablename__ = "translation_suggestions"
    __audit__ = True

    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: One of the draft languages: `pt-BR`, `es-ES`, `sv-SE`.
    locale: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The message's `msgctxt`, or "" for the many that have none.
    context: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    #: The message's English source, its `msgid`.
    message: Mapped[str] = mapped_column(Text, nullable=False)
    suggested: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    suggested_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[SuggestionStatus] = mapped_column(
        EnumStr(SuggestionStatus, 16), nullable=False, default=SuggestionStatus.open
    )

    household: Mapped[Household] = relationship("Household", back_populates="translation_suggestions")


class Receipt(Base, UUIDPrimaryKey, Timestamped):
    """One receipt: the act of deciding that this image is evidence for this row.

    The bytes are not here -- see :class:`ReceiptBlob`. What is here is what
    people change: which transaction it belongs to, the note somebody wrote on
    it, and what the camera said about where and when it was taken.

    Everything derivable is derived. The filename it downloads as is computed
    from live state (``services/receipts.py::display_name``) rather than
    stored, so re-matching a receipt renames it for free; the previous build
    stored an absolute path and broke on every move of the data directory.
    """

    __tablename__ = "receipts"
    __audit__ = True

    #: RESTRICT for the same reason as `transactions.household_id`: a household
    #: reaches its receipts through the ORM relationship below, which the audit
    #: hook walks. A second, database-level path would delete the same rows
    #: without the hook hearing about it.
    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Nullable on purpose: null IS the inbox. A photo taken at the till days
    #: before the statement posts is a real state, not a missing one -- the same
    #: reasoning `transactions.category_id` already carries.
    #:
    #: SET NULL, not CASCADE: deleting a transaction must not destroy the
    #: evidence for it. The hook's `_nullify_referencing_children` turns that
    #: into a logged ORM update, so undoing the delete re-attaches the receipt.
    transaction_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("transactions.id", ondelete="SET NULL"), index=True
    )
    #: SHA-256 of the ORIGINAL bytes, hex. **The dedupe key, and only that.**
    #: Taken before any re-encode, so an encoder that changes by one byte
    #: between Pillow releases does not make every stored receipt stop matching
    #: itself -- and supplied by the client on the mobile path, where the phone
    #: compresses before sending and the server never sees the original.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: SHA-256 of the bytes this server actually received, hex. **The pointer
    #: into the blob store**, and never anything a client can choose.
    #:
    #: The two are the same on every upload the desktop makes and differ on
    #: every one the phone makes. They are separate columns because merging
    #: them is a cross-household read: `content_sha256` is client-supplied, so
    #: a lie about it would select which stored bytes the new receipt points
    #: at, and the picture served back would be somebody else's. That is not
    #: hypothetical -- it is what this code did until a test asked for the
    #: image back and got a different one.
    blob_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: What the browser called it. Provenance only: never used to decide
    #: anything, never echoed into a Content-Type, never treated as a path.
    original_filename: Mapped[str | None] = mapped_column(String(300))
    #: The SNIFFED type. This is what Content-Type is served from.
    media_type: Mapped[str] = mapped_column(String(80), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    #: How many pages a PDF has, null for a picture. Stored rather than
    #: derived because deriving it means re-parsing the PDF on every panel
    #: open, and it changes what the frame says: most receipt PDFs in a real
    #: corpus are multi-page, some run to dozens of pages, so "page 1 of 81 --
    #: download for the rest" is the difference between a preview and a lie.
    page_count: Mapped[int | None] = mapped_column(Integer)

    #: Parsed out of EXIF at ingest, and then stripped from every byte this app
    #: serves. The database is the record; the file carries nothing.
    #:
    #: Naive local time where the camera gave no OffsetTimeOriginal, and
    #: `captured_at_is_local` says which -- guessing UTC moves a receipt across
    #: midnight and onto the day the statement will not match.
    captured_at: Mapped[datetime | None] = mapped_column(DateTime)
    captured_at_is_local: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: Where it was taken. All nullable and usually null: a screenshot, a scan,
    #: a PDF and anything a messaging app has touched carry none of this.
    #:
    #: Signed decimal degrees, converted at ingest from the EXIF's
    #: degrees/minutes/seconds triple and its N/S/E/W ref -- stored converted so
    #: no reader re-implements the conversion and forgets the hemisphere.
    gps_lat: Mapped[float | None] = mapped_column(Float)
    gps_lon: Mapped[float | None] = mapped_column(Float)
    #: Metres, from GPSHPositioningError. A 4 m fix and a 2 km cell-tower fix
    #: are both "GPS" and must not be drawn the same way.
    gps_accuracy_m: Mapped[float | None] = mapped_column(Float)
    #: Degrees true, from GPSImgDirection. Which way the camera faced.
    gps_bearing: Mapped[float | None] = mapped_column(Float)
    #: Make + Model, joined. "Which of us photographed this" -- and unlike
    #: uploaded_by_id it is a property of the file rather than of the session.
    camera: Mapped[str | None] = mapped_column(String(120))
    #: Every remaining scalar tag, ~1.7 KiB of JSON.
    #:
    #: Named `exif` and not `metadata`: `Base.metadata` is SQLAlchemy's own
    #: MetaData object, and a mapped column of that name is refused at import.
    #: The name is also the more honest one -- this is the camera's block, not
    #: the application's bookkeeping.
    #:
    #: MakerNote, the embedded EXIF thumbnail and every other binary field are
    #: DROPPED, not redacted: the raw block is ~30 KiB and one of the things in
    #: it is a miniature of the receipt, which in an audited table would land in
    #: changes.before AND changes.after on every edit.
    exif: Mapped[dict | None] = mapped_column(JSON)
    #: True when the bytes we received had already been through an encoder --
    #: the mobile capture page compresses before it uploads. Its own column
    #: rather than a key in `exif`, because `exif` holds what the camera wrote
    #: and this is a fact about the upload.
    client_encoded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: A person's note. The only free text anybody edits.
    note: Mapped[str | None] = mapped_column(Text)
    #: What an agent read off the image -- merchant, total, tax, line items.
    #:
    #: **A CLAIM, and never truth.** Nothing in the ledger is derived from it:
    #: no amount, no date, no payee, no category. It is stored beside the
    #: picture so a person can compare the two, and so a later tool can improve
    #: on it without re-reading every image.
    #:
    #: The app does no OCR and calls no model. Putting extraction in here would
    #: mean tesseract or a vision API inside a self-hosted tailnet ledger, and
    #: a dependency heavier than everything in `requirements.txt` combined --
    #: so the agent reads the image, which is what it is for, and posts what it
    #: saw. `/candidates` deliberately does NOT match on this: a suggestion
    #: built from a guess would launder the guess into a decision.
    extracted: Mapped[dict | None] = mapped_column(JSON)
    #: Who put it here. Not the batch actor: an undo re-inserts the row, and the
    #: original uploader should survive that.
    uploaded_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL")
    )

    household: Mapped[Household] = relationship("Household", back_populates="receipts")
    transaction: Mapped[Transaction | None] = relationship(
        "Transaction", back_populates="receipts"
    )

    __table_args__ = (
        Index("ix_receipts_household_txn", "household_id", "transaction_id"),
        Index("ix_receipts_inbox", "household_id", "created_at"),
        #: The "same file twice" rule, enforced by the database rather than by a
        #: check in a service that a second code path can miss. Scoped to the
        #: transaction, not the household: one PDF can legitimately be evidence
        #: for two rows, and a split copies its receipt onto every part.
        UniqueConstraint(
            "transaction_id", "content_sha256", name="uq_receipts_txn_content"
        ),
        #: Not for today's screens -- for "what else did we buy near here",
        #: which is one query once the column is indexed and an afternoon's
        #: migration if it is not.
        Index("ix_receipts_gps", "household_id", "gps_lat", "gps_lon"),
    )


class ReceiptBlob(Base):
    """Immutable bytes, addressed by what they contain.

    Deliberately **out** of the audit log. A row here is inserted once, never
    updated in any way a reader would care about, and deleted only when nothing
    references it -- so there is no history worth keeping. There is also a
    concrete cost if there were: ``audit/snapshot.py`` writes complete rows on
    purpose so undo stays a plain assignment, and base64s ``bytes``. A 175 KiB
    receipt would cost 233 KiB of log on insert and 466 KiB on every update;
    three memo edits and the log would hold more of the picture than the store.
    """

    __tablename__ = "receipt_blobs"
    __audit__ = False

    #: SHA-256 of the ORIGINAL bytes, hex. Two households uploading the same
    #: PDF store it once, and neither can tell.
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[BlobRole] = mapped_column(EnumStr(BlobRole, 16), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    media_type: Mapped[str] = mapped_column(String(80), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    #: When the sweep first found nothing pointing at this row, or null while
    #: something still does.
    #:
    #: The grace period has to be measured from when the blob was *orphaned*,
    #: not from when it was created: a blob uploaded a week ago whose last
    #: receipt was deleted a minute ago is exactly the one an undo is about to
    #: need, and `created_at` would sweep it immediately. See
    #: `services/receipts.py::sweep_orphan_blobs`.
    orphaned_at: Mapped[datetime | None] = mapped_column(DateTime)
