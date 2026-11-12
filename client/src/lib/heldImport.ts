/**
 * A bank file dropped on the first-run wizard's account step (#458), held in
 * the page until the Import screen opens after setup.
 *
 * It is not imported in the wizard: nothing is saved until the last step,
 * and the import preview needs the account to exist. The Import screen takes
 * it once, as if it had been chosen there, so the usual preview applies.
 * Memory only: a reload forgets it, as it forgets the rest of the wizard.
 */

let held: File | null = null;

export function holdImportFile(file: File | null): void {
  held = file;
}

/** The held file, once: taking it lets it go. */
export function takeImportFile(): File | null {
  const file = held;
  held = null;
  return file;
}
