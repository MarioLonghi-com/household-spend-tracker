import { describe, expect, it } from "vitest";
import { Backend } from "../src/backend";

/** A backend that starts and then says nothing at all, ever (#442). */
function silent(): Backend {
  return new Backend({ command: process.execPath, args: ["-e", "setInterval(() => {}, 1000)"], cwd: process.cwd(), env: process.env });
}

/** One that writes one frame and then nothing: `{"id": 0, "event": "upgrading", "steps": 2}`. */
function upgradingForever(): Backend {
  const script = `
    const head = Buffer.from(JSON.stringify({ id: 0, event: "upgrading", steps: 2 }));
    const frame = (b) => { const n = Buffer.alloc(4); n.writeUInt32BE(b.length); return Buffer.concat([n, b]); };
    process.stdout.write(Buffer.concat([frame(head), frame(Buffer.alloc(0))]));
    setInterval(() => {}, 1000);
  `;
  return new Backend({ command: process.execPath, args: ["-e", script], cwd: process.cwd(), env: process.env });
}

describe("a backend that stops answering", () => {
  it("a start that says nothing ends as died, and stopping it kills it", async () => {
    const backend = silent();
    const started = await backend.started(undefined, 200);
    expect(started.kind).toBe("died");
    expect(started.kind === "died" && started.said).toContain("said nothing for 0.2 s");
    expect(backend.alive).toBe(true);
    await backend.stop(200);
    expect(backend.alive).toBe(false);
  });

  it("a start that is upgrading is waited for however long it takes", async () => {
    const backend = upgradingForever();
    let steps = 0;
    const outcome = await Promise.race([
      backend.started((n) => (steps = n), 200),
      new Promise<"still waiting">((resolve) => setTimeout(() => resolve("still waiting"), 600)),
    ]);
    expect(outcome).toBe("still waiting");
    expect(steps).toBe(2);
    await backend.stop(200);
  });

  it("a restore that says nothing ends as died", async () => {
    const backend = silent();
    const answer = await backend.restored(200);
    expect(answer.kind).toBe("died");
    await backend.stop(200);
  });

  it("a control request with no answer comes back as not answered", async () => {
    const backend = silent();
    const answer = await backend.control<{ detail: string }>("state", 200);
    expect(answer).toEqual({ status: 0, data: { detail: "the backend is not running, or did not answer" } });
    await backend.stop(200);
  });
});
