/**
 * Bytes, at the scale a person reads them.
 *
 * Binary units, and labelled as such: a database is a file on a disk and every
 * tool that will be used to look at it beside this screen — `ls -lh`, Finder's
 * inspector — is going to disagree with anything else by 2.4%, which is
 * exactly enough to make somebody think one of them is wrong.
 */
export const bytes = (n: number | null): string => {
  if (n === null) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = n / 1024;
  let at = 0;
  while (value >= 1024 && at < units.length - 1) {
    value /= 1024;
    at += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[at]}`;
};
