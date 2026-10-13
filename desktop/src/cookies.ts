/**
 * The session's cookies, held by the shell in memory (#59, #320).
 *
 * Chromium does not store a `Set-Cookie` from a `protocol.handle` response,
 * so the page could not stay signed in. The shell keeps them instead: it takes
 * `Set-Cookie` off the backend's answers and adds `Cookie` to the requests it
 * forwards. The page never sees either, and nothing is written to disk -- so
 * a session lasts exactly as long as the app runs, which is what "a password
 * at every launch" asks for.
 */

type Kept = { value: string; expires: number | null };

export class CookieJar {
  private cookies = new Map<string, Kept>();

  constructor(private readonly now: () => number = Date.now) {}

  take(setCookies: readonly string[]): void {
    for (const line of setCookies) {
      const [pair, ...attributes] = line.split(";").map((part) => part.trim());
      const eq = pair.indexOf("=");
      if (eq <= 0) continue;
      const name = pair.slice(0, eq);
      const value = pair.slice(eq + 1);
      let expires: number | null = null;
      let maxAge = false;
      for (const attribute of attributes) {
        const at = attribute.indexOf("=");
        const key = (at === -1 ? attribute : attribute.slice(0, at)).toLowerCase();
        const raw = at === -1 ? "" : attribute.slice(at + 1);
        if (key === "max-age") {
          expires = this.now() + Number(raw) * 1000;
          maxAge = true;
        } else if (key === "expires" && !maxAge) {
          expires = Date.parse(raw);
        }
      }
      if (expires !== null && expires <= this.now()) this.cookies.delete(name);
      else this.cookies.set(name, { value, expires });
    }
  }

  header(): string {
    const live: string[] = [];
    for (const [name, { value, expires }] of this.cookies) {
      if (expires !== null && expires <= this.now()) this.cookies.delete(name);
      else live.push(`${name}=${value}`);
    }
    return live.join("; ");
  }

  names(): string[] {
    return [...this.cookies.keys()];
  }

  clear(): void {
    this.cookies.clear();
  }
}
