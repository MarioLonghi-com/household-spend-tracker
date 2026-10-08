# Glossary: pt-BR, es-ES, sv-SE

One agreed rendering per term, so every screen translates the same word the
same way. The draft translations (#175 pt-BR, #176 es-ES, #177 sv-SE) follow
this table; when a draft needs a word that is not here, add the row first.

**Every row is a draft.** Nothing here has been reviewed by a native speaker.
That review is #58, and it is what turns *draft* into *reviewed*. Until then a
translator may propose a better word on the issue -- change the row, then the
catalogs, never only the catalogs.

The English terms are the ones the screens use today, taken from the code
rather than invented. Where the English itself says one thing two ways, the row
says so: those are the places a translation is most likely to drift.

## Register and tone

| | pt-BR | es-ES | sv-SE |
|---|---|---|---|
| Address | *você*, never *tu* or *o senhor* | *tú* (see below) | *du*, never *ni* |
| Case | Sentence case: only the first word and names capitalised, in headings, buttons and menu items alike | Same | Same |
| Imperatives on buttons | Infinitive or imperative, short: *Salvar*, *Importar extrato* | Infinitive: *Guardar*, *Importar extracto* | Imperative: *Spara*, *Importera kontoutdrag* |
| Tone | Plain and direct, as the English is. No exclamation marks | Same | Same |

**Why *tú* in es-ES.** Consumer finance in Spain addresses its users as *tú*:
the large retail banks' apps, the neobanks and the budgeting apps all do, and
*usted* now reads as a letter from a tax office or an older bank's terms and
conditions. This app is used by the people of one household about their own
money, which is the most informal setting there is. *Usted* would also sit
badly beside an English original that says "you" plainly and often. If a
native reviewer disagrees in #58, the change is mechanical but touches every
message, so it is worth settling early.

The English often speaks in full sentences where other apps use a label
("Cleared — the bank has it", "Work should pay this back"). Keep the sentence.
The sentence is the design: it says what the state means, not only its name.

## Numbers, money and dates

None of these are written by a translator. `lib/locale.ts` formats them
through `Intl` (#52); a message carries a placeholder, never a pre-formatted
figure. This section is here so a reviewer recognises a wrong one.

| | pt-BR | es-ES | sv-SE |
|---|---|---|---|
| Money | `R$ 1.234,56` | `1234,56 €` | `1 234,56 kr` |
| Money, 10 000 and up | `R$ 12.345,67` | `12.345,67 €` -- es-ES groups only from 10 000 | `12 345,67 kr` |
| Negative | `-R$ 1.234,56` | `-1234,56 €` | `−1 234,56 kr` -- U+2212 MINUS SIGN, not a hyphen |
| Space before the symbol or code | U+00A0 after `R$` | U+00A0 before `€` | U+00A0 before `kr`, and U+00A0 as the thousands separator |
| Plain number | `12.345,6` | `12.345,6` | `12 345,6` |
| Date | `07/10/2026` | `07/10/2026` | `2026-10-07` |
| Month and year | `outubro de 2026` | `octubre de 2026` | `oktober 2026` |
| Month names | lower case | lower case | lower case |

A message never puts a currency symbol next to a placeholder: the amount
arrives with its own, in the right place for the locale.

## Words that stay in English

- **Spend Tracker**, the product name.
- **YNAB**, and other products named on screen.
- Currency codes (`EUR`, `GBP`, `BRL`, `SEK`), country codes, and **IBAN**.
- File formats: **CSV**, **OFX**, **QIF**, **PDF**, **XLSX**.
- What a bank or another program calls something, when the screen quotes it.
- A household's, account's, payee's or category's own name. They are data.

## Terms

Sorted by English term. *Where* links to the place the English is defined
today; once #52 lands, labels move to `lib/labels.ts` and the links follow.
*Status* is `draft` on every row until #58.

| English | Where | pt-BR | es-ES | sv-SE | Note | Status |
|---|---|---|---|---|---|---|
| account | [Accounts](../screens/Accounts.tsx) | conta | cuenta | konto | | draft |
| account type | [Accounts `TYPES`](../screens/Accounts.tsx) | tipo de conta | tipo de cuenta | kontotyp | Six types, each with a behaviour; the rows below name them | draft |
| Accounts (screen) | [App menu](../App.tsx) | Contas | Cuentas | Konton | | draft |
| Admin (menu section) | [App menu](../App.tsx) | Administração | Administración | Administration | | draft |
| Already have it (import outcome `matched_existing`) | [Import `OUTCOME_WORDS`](../screens/Import.tsx) | Já existe | Ya la tienes | Finns redan | The row matched one already in the ledger, entered by hand or by another import | draft |
| Already imported (import outcome `duplicate_skipped`) | [Import `OUTCOME_WORDS`](../screens/Import.tsx) | Já importada | Ya importada | Redan importerad | Feminine in pt and es: it agrees with *transação* / *transacción* | draft |
| amount | [Register](../screens/Register.tsx) | valor | importe | belopp | es-ES banks say *importe*; *monto* is Latin American, *cantidad* means a quantity | draft |
| Appearance | [Profile](../screens/Profile.tsx) | Aparência | Apariencia | Utseende | | draft |
| Application management | [App menu](../App.tsx) | Gerenciamento do aplicativo | Gestión de la aplicación | Hantering av appen | About the installation, not a ledger: owner only | draft |
| archived (category, payee) | [History field names](../../../app/services/describing.py) | arquivado | archivado | arkiverad | Agrees with its noun; the History word is *arquivado* / *archivado* | draft |
| authenticator | [Profile](../screens/Profile.tsx), [StepUp](../components/StepUp.tsx) | aplicativo autenticador | aplicación de autenticación | autentiseringsapp | The app on a phone that shows six digits. Not the passkey | draft |
| authenticator code | [StepUp](../components/StepUp.tsx) | código do autenticador | código del autenticador | kod från autentiseringsappen | "The six digits from your authenticator" | draft |
| backup | [Backups](../screens/Backups.tsx) | backup | copia de seguridad | säkerhetskopia | pt-BR uses the English word; *cópia de segurança* reads as European Portuguese | draft |
| balance | [Accounts](../screens/Accounts.tsx), [Reconcile](../screens/Reconcile.tsx) | saldo | saldo | saldo | | draft |
| bank (account's institution) | [History field names](../../../app/services/describing.py) | banco | banco | bank | Stored as `institution`; History calls it "bank" | draft |
| base currency | [Household](../screens/Household.tsx) | moeda base | divisa base | basvaluta | | draft |
| batch (History) | [History](../screens/History.tsx) | lote | lote | omgång | One act in History, undone as a whole | draft |
| Cash (account type `cash`) | [Accounts `TYPES`](../screens/Accounts.tsx) | Dinheiro | Efectivo | Kontanter | | draft |
| category | [Categories](../screens/Categories.tsx) | categoria | categoría | kategori | | draft |
| category group | [Categories](../screens/Categories.tsx) | grupo de categorias | grupo de categorías | kategorigrupp | | draft |
| Cleared (state `cleared`) | [Register `CLEARED_PILLS`](../screens/Register.tsx) | Compensada | Contabilizada | Bokförd | "the bank has it". es-ES and sv-SE use the word their banks show for a posted movement; *conciliada* is reconcile and must not be used here | draft |
| closed (account) | [Accounts](../screens/Accounts.tsx) | encerrada | cerrada | avslutat | Agrees with *conta* / *cuenta* / *konto* | draft |
| country | [Accounts](../screens/Accounts.tsx) | país | país | land | Country names come from `Intl.DisplayNames`, never from the catalog | draft |
| Credit card (account type `credit_card`) | [Accounts `TYPES`](../screens/Accounts.tsx) | Cartão de crédito | Tarjeta de crédito | Kreditkort | The filter heading says "Credit cards" (plural); same type | draft |
| currency | [Accounts](../screens/Accounts.tsx) | moeda | divisa | valuta | es-ES: *divisa* is the word Spanish banks use for an account's currency; *moneda* is a coin | draft |
| Current account (account type `checking`) | [Accounts `TYPES`](../screens/Accounts.tsx) | Conta corrente | Cuenta corriente | Lönekonto | The English says "Current account" in the form and "Checking" in the filter heading ([accountGroups](../components/accountGroups.ts)); translate both as this row. sv-SE banks also say *privatkonto*; *lönekonto* is the most widely understood | draft |
| date | [Register](../screens/Register.tsx) | data | fecha | datum | | draft |
| default category | [Payee categorisation](../screens/PayeeCategorisation.tsx) | categoria padrão | categoría predeterminada | standardkategori | | draft |
| device, this device | [Profile](../screens/Profile.tsx) | dispositivo, este dispositivo | dispositivo, este dispositivo | enhet, den här enheten | Language and format are chosen per device (#48) | draft |
| disable, re-enable (a person's sign-in) | [Admin](../screens/Admin.tsx) | desativar, reativar | desactivar, reactivar | inaktivera, återaktivera | Stops or allows signing in; nothing is deleted | draft |
| exchange rate | [Transfer](../screens/Transfer.tsx) | taxa de câmbio | tipo de cambio | växelkurs | | draft |
| History | [History](../screens/History.tsx) | Histórico | Historial | Historik | The household's record of every change, with Undo | draft |
| household | [Household](../screens/Household.tsx) | casa | hogar | hushåll | The shared ledger of the people who live together. pt-BR *domicílio* is a census word; *família* assumes too much | draft |
| How import works | [App menu](../App.tsx), [ImportGuide](../screens/ImportGuide.tsx) | Como funciona a importação | Cómo funciona la importación | Så fungerar importen | | draft |
| IBAN | [Accounts](../screens/Accounts.tsx) | IBAN | IBAN | IBAN | Stays | draft |
| import (verb) | [Import](../screens/Import.tsx) | importar | importar | importera | | draft |
| import (noun) | [Import](../screens/Import.tsx) | importação | importación | import | | draft |
| Import a statement | [Import](../screens/Import.tsx) | Importar um extrato | Importar un extracto | Importera ett kontoutdrag | | draft |
| Income vs Expense (report) | [Reports `REPORTS`](../screens/Reports.tsx) | Receitas e despesas | Ingresos y gastos | Inkomster och utgifter | The English says "Income vs Expense" in the menu and "Income v Expense" in its own heading and in prose; translate both as this row | draft |
| instance | [Application management](../screens/ApplicationManagement.tsx) | instância | instancia | instans | One installation of the app, holding every household | draft |
| invite, invitation | [Household](../screens/Household.tsx), [AcceptInvite](../screens/AcceptInvite.tsx) | convidar, convite | invitar, invitación | bjuda in, inbjudan | | draft |
| Keys for programs | [Profile](../screens/Profile.tsx) | Chaves para programas | Claves para programas | Nycklar för program | API keys for agents. es-ES: *clave*, kept apart from the passkey's *llave* | draft |
| ledger | many sentences | livro-caixa | libro | boken | Everything recorded for a household; sv takes the definite form in a sentence | draft |
| leg (of a transfer) | [History field names](../../../app/services/describing.py), refusals | lado | lado | ben | One of the two rows a transfer is made of; also said "side" | draft |
| Locked (state `reconciled`) | [Register `CLEARED_PILLS`](../screens/Register.tsx) | Bloqueada | Bloqueada | Låst | The stored value is `reconciled`; the screen says "Locked" | draft |
| match (verb, import and transfers) | [ImportGuide](../screens/ImportGuide.tsx) | corresponder | emparejar | matcha | Two rows found to be the same thing | draft |
| member (role `member`) | [Household](../screens/Household.tsx) | membro | miembro | medlem | | draft |
| memo | [Register](../screens/Register.tsx) | descrição | concepto | beskrivning | es-ES statements call the bank's text *concepto*, which is what an import puts here. Not *nota*: that is **note** | draft |
| merge (payees) | [Payees](../screens/Payees.tsx) | unir; união | fusionar; fusión | slå ihop; sammanslagning | Two payees become one | draft |
| Needs a look (import outcome `needs_review`) | [Import `OUTCOME_WORDS`](../screens/Import.tsx) | Precisa de revisão | Hay que revisarla | Behöver granskas | | draft |
| New (import outcome `created`) | [Import `OUTCOME_WORDS`](../screens/Import.tsx) | Nova | Nueva | Ny | Agrees with *transação* / *transacción* / *transaktion* | draft |
| Not a work expense | [`REIMBURSEMENT_LABELS`](../lib/reimbursement.ts) | Não é despesa de trabalho | No es un gasto de trabajo | Inget jobbutlägg | | draft |
| Not for this account (import outcome `skipped`) | [Import `OUTCOME_WORDS`](../screens/Import.tsx) | Não é desta conta | No es de esta cuenta | Inte för det här kontot | | draft |
| note (category, receipt) | [History field names](../../../app/services/describing.py), [Receipts](../screens/Receipts.tsx) | nota | nota | anteckning | Not **memo** | draft |
| Opening balance | [Accounts](../screens/Accounts.tsx) | Saldo inicial | Saldo inicial | Ingående saldo | | draft |
| Other asset (account type `other_asset`) | [Accounts `TYPES`](../screens/Accounts.tsx) | Outro ativo | Otro activo | Övrig tillgång | Filter heading: "Other assets" | draft |
| Other debt (account type `other_liability`) | [Accounts `TYPES`](../screens/Accounts.tsx) | Outra dívida | Otra deuda | Övrig skuld | The form says "Other debt", the filter heading "Other liabilities"; translate both as this row | draft |
| owner (role `owner`) | [Household](../screens/Household.tsx) | proprietário | propietario | ägare | Not *titular*: that is **holder** | draft |
| passkey | [Passkeys](../screens/Passkeys.tsx), [SignIn](../screens/SignIn.tsx) | chave de acesso | llave de acceso | lösennyckel | The words the platforms' own passkey prompts use, so the app and the system dialog agree. es-ES *llave*, kept apart from *clave* (password, API key); sv-SE *lösennyckel*, kept apart from *nyckel* (an agent's key) | draft |
| password | [SignIn](../screens/SignIn.tsx) | senha | contraseña | lösenord | | draft |
| payee | [Payees](../screens/Payees.tsx) | favorecido | beneficiario | mottagare | Also the one who pays an income in (an employer). The bank word is kept because it is the one people know from their statements | draft |
| Payee Categorisation (screen) | [App menu](../App.tsx) | Categorização de favorecidos | Categorización de beneficiarios | Kategorisering av mottagare | The menu says "Payee Categorisation" (title case), the heading "Payee categorisation"; sentence case in all three | draft |
| Payee Merge (screen) | [App menu](../App.tsx) | Unir favorecidos | Fusionar beneficiarios | Slå ihop mottagare | The heading of the screen is "Payees" | draft |
| Payee Naming Rules (screen) | [App menu](../App.tsx), [Rules](../screens/Rules.tsx) | Regras de nome de favorecidos | Reglas de nombre de beneficiarios | Namnregler för mottagare | | draft |
| Profile | [Profile](../screens/Profile.tsx) | Perfil | Perfil | Profil | | draft |
| receipt | [Receipts](../screens/Receipts.tsx) | comprovante | ticket | kvitto | es-ES: *recibo* in Spain is a bill paid by direct debit; a shop's slip is a *ticket*. pt-BR: *nota fiscal* is the tax invoice, not every slip | draft |
| reconcile, reconciliation | [Reconcile](../screens/Reconcile.tsx) | conciliar, conciliação | conciliar, conciliación | stämma av, avstämning | Checking the ledger against a statement and locking what matched | draft |
| recovery code | [RecoveryCodes](../components/RecoveryCodes.tsx), [SignIn](../screens/SignIn.tsx) | código de recuperação | código de recuperación | återställningskod | sv-SE: *återställ* also means restore; context separates them | draft |
| Register (menu section) | [App menu](../App.tsx) | Registro | Registro | Register | The section holding Transactions, Import and Receipts | draft |
| regular expression, pattern | [Payee Naming Rules](../screens/Rules.tsx) | expressão regular, padrão | expresión regular, patrón | reguljärt uttryck, mönster | A rule's text to match | draft |
| Reimbursements (report) | [Reports `REPORTS`](../screens/Reports.tsx) | Reembolsos | Reembolsos | Utlägg | sv-SE: *utlägg* is the word for an expense an employer pays back | draft |
| Reports | [Reports](../screens/Reports.tsx) | Relatórios | Informes | Rapporter | es-ES *informes*; *reportes* is Latin American | draft |
| reset link | [Admin](../screens/Admin.tsx) | link de redefinição | enlace de restablecimiento | återställningslänk | One-time link for choosing a new password or authenticator | draft |
| restore (a backup) | [Backups](../screens/Backups.tsx) | restaurar | restaurar | återställa | | draft |
| revoke | [Profile](../screens/Profile.tsx) | revogar | revocar | återkalla | A key or device stops working at once | draft |
| role | [Household](../screens/Household.tsx), [Admin](../screens/Admin.tsx) | função | rol | roll | Owner or member | draft |
| rule | [Rules](../screens/Rules.tsx) | regra | regla | regel | | draft |
| Savings (account type `savings`) | [Accounts `TYPES`](../screens/Accounts.tsx) | Poupança | Ahorro | Sparkonto | | draft |
| setup token | [Setup](../screens/Setup.tsx) | token de configuração | token de configuración | installationstoken | | draft |
| sign in, sign out | [SignIn](../screens/SignIn.tsx) | entrar, sair | iniciar sesión, cerrar sesión | logga in, logga ut | | draft |
| sign-in methods | [Profile](../screens/Profile.tsx) | formas de entrar | métodos de inicio de sesión | inloggningsmetoder | | draft |
| Snap a Receipt | [App menu](../App.tsx) | Fotografar um comprovante | Fotografiar un ticket | Fota ett kvitto | Title case in the menu; sentence case in translation | draft |
| split (noun), split (verb) | [SplitBar](../components/SplitBar.tsx) | divisão, dividir | división, dividir | uppdelning, dela upp | One transaction across several categories | draft |
| statement | [Import](../screens/Import.tsx), [Reconcile](../screens/Reconcile.tsx) | extrato | extracto | kontoutdrag | | draft |
| step-up (confirm with password and code) | [StepUp](../components/StepUp.tsx) | confirmar sua identidade | confirmar tu identidad | bekräfta din identitet | The English never names it; it says "this asks for your password and a code". Use the phrase, not a noun | draft |
| transaction | [Register](../screens/Register.tsx) | transação | movimiento | transaktion | es-ES banks list *movimientos*; *transacción* reads as a translation | draft |
| Transactions (screen) | [App menu](../App.tsx) | Transações | Movimientos | Transaktioner | | draft |
| transfer | [Transfer](../screens/Transfer.tsx), [Transfers](../screens/Transfers.tsx) | transferência | transferencia | överföring | Between the household's own accounts only; not spending, no category | draft |
| Uncategorised | [History field names](../../../app/services/describing.py) | Sem categoria | Sin categoría | Okategoriserad | | draft |
| Uncleared (state `uncleared`) | [Register `CLEARED_PILLS`](../screens/Register.tsx) | Não compensada | Pendiente | Ej bokförd | "the bank has not seen this yet" | draft |
| undo | [History](../screens/History.tsx) | desfazer | deshacer | ångra | | draft |
| work expense; Work should pay this back (state `expected`) | [`REIMBURSEMENT_LABELS`](../lib/reimbursement.ts), [History field names](../../../app/services/describing.py) | despesa de trabalho; O trabalho deve reembolsar | gasto de trabajo; El trabajo debería devolverlo | jobbutlägg; Jobbet ska betala tillbaka | Keep the sentence: "expected" alone does not say by whom | draft |
| Written off (state `written_off`) | [`REIMBURSEMENT_LABELS`](../lib/reimbursement.ts) | Dada como perdida | Dado por perdido | Avskriven | "work will not pay it" | draft |
| YNAB | [OneTimeImport](../screens/OneTimeImport.tsx) | YNAB | YNAB | YNAB | Stays | draft |
