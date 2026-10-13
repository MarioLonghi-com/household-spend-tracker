/**
 * The backend process: the frozen FastAPI app, spoken to over stdin/stdout.
 *
 * One `Backend` is one run of the child. It is started, says how its start
 * went in a frame with id 0 (`upgrading` while it migrates, then `ready` or
 * `stopped`), serves requests and the shell's own `control` requests, and
 * stops when its stdin closes -- the app's shutdown runs, the WAL is folded
 * back in, and the process exits. A restore is a run of its own that answers
 * `restored` or `stopped` and exits.
 *
 * Its standard error is the log of everything before the app's own logging
 * starts (the migrations, a refusal to start), so it is kept: the last part in
 * memory for a dialog, all of it in the shell's log file.
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import type { Writable } from "node:stream";
import { encode, FrameReader, type Head, type Message } from "./frames";

/** What `app.desktop.launch.prepare` said, as the adapter sends it. */
export type Launch = {
  outcome: string;
  stamp_before?: string | null;
  stamp_after?: string | null;
  migrated?: string[];
  lossy?: string[];
  backup?: string | null;
  dropped?: string[];
  log?: string[];
};

/** What `app.desktop.restore.restore` said. */
export type RestoreReport = {
  outcome: string;
  reason: string;
  revision?: string | null;
  rows?: number;
  aside?: string[];
};

export type Started =
  | { kind: "ready"; launch: Launch }
  | { kind: "stopped"; launch: Launch }
  | { kind: "died"; code: number | null; said: string };

export type RestoreAnswer =
  | { kind: "restored"; restore: RestoreReport }
  | { kind: "stopped"; restore: RestoreReport }
  | { kind: "died"; code: number | null; said: string };

export type Spawned = {
  command: string;
  args: string[];
  cwd: string;
  env: NodeJS.ProcessEnv;
  /** Where standard error is copied, as it arrives. */
  log?: Writable;
};

/** How much of standard error a dialog can show. */
const KEEP_SAID = 16 * 1024;

export class Backend {
  private readonly child: ChildProcessWithoutNullStreams;
  private readonly reader = new FrameReader();
  private readonly pending = new Map<number, (message: Message | null) => void>();
  private readonly events: ((head: Head) => void)[] = [];
  private next = 1;
  private said = "";
  private exited: Promise<number | null>;
  private running = true;

  constructor(spawned: Spawned) {
    this.child = spawn(spawned.command, spawned.args, {
      cwd: spawned.cwd,
      env: spawned.env,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    this.child.stdout.on("data", (chunk: Buffer) => this.take(chunk));
    this.child.stderr.on("data", (chunk: Buffer) => {
      this.said = (this.said + chunk.toString("utf8")).slice(-KEEP_SAID);
      spawned.log?.write(chunk);
    });
    // A child that cannot start at all (no such file) emits `error` and no
    // `exit`; both end in the same place.
    this.exited = new Promise((resolve) => {
      this.child.once("exit", (code) => resolve(code));
      this.child.once("error", (problem) => {
        this.said += `\n${problem.message}`;
        resolve(null);
      });
    });
    void this.exited.then(() => {
      this.running = false;
      for (const waiting of this.pending.values()) waiting(null);
      this.pending.clear();
    });
    // Writing to a child that has gone is an EPIPE on the stream, not a throw.
    this.child.stdin.on("error", () => undefined);
  }

  /** The process's executable, as the operating system started it. */
  get executable(): string {
    return this.child.spawnfile;
  }

  get alive(): boolean {
    return this.running;
  }

  /** The last of what the backend wrote to standard error. */
  get lastSaid(): string {
    return this.said;
  }

  /** Called once with the exit code when the process ends, however it ends. */
  onExit(listener: (code: number | null) => void): void {
    void this.exited.then(listener);
  }

  private take(chunk: Buffer): void {
    for (const message of this.reader.push(chunk)) {
      if (message.head.id === 0) {
        for (const listener of this.events) listener(message.head);
        continue;
      }
      const waiting = this.pending.get(message.head.id);
      if (waiting) {
        this.pending.delete(message.head.id);
        waiting(message);
      }
    }
  }

  /**
   * How the start went. `upgrading` is told the number of steps when an
   * existing ledger is being migrated, before the answer.
   */
  started(upgrading: (steps: number) => void = () => undefined): Promise<Started> {
    return new Promise((resolve) => {
      this.events.push((head) => {
        if (head.event === "upgrading") upgrading(Number(head.steps) || 0);
        else if (head.event === "ready") resolve({ kind: "ready", launch: head.launch as Launch });
        else if (head.event === "stopped") resolve({ kind: "stopped", launch: head.launch as Launch });
      });
      void this.exited.then((code) => resolve({ kind: "died", code, said: this.said }));
    });
  }

  /** The answer of a run started with `SPENDTRACKER_RESTORE_FROM`. */
  restored(): Promise<RestoreAnswer> {
    return new Promise((resolve) => {
      this.events.push((head) => {
        const restore = head.restore as RestoreReport;
        if (head.event === "restored") resolve({ kind: "restored", restore });
        else if (head.event === "stopped") resolve({ kind: "stopped", restore });
      });
      void this.exited.then((code) => resolve({ kind: "died", code, said: this.said }));
    });
  }

  /** One request; null when the backend is gone before it answered. */
  send(head: Omit<Head, "id">, body: Buffer = Buffer.alloc(0)): Promise<Message | null> {
    if (!this.running) return Promise.resolve(null);
    const id = this.next++;
    return new Promise((resolve) => {
      this.pending.set(id, resolve);
      this.child.stdin.write(encode({ ...head, id }, body));
    });
  }

  /** One of the shell's own requests (`app/desktop/control.py`). */
  async control<T>(name: string): Promise<{ status: number; data: T }> {
    const answer = await this.send({ control: name });
    if (answer === null) return { status: 0, data: { detail: "the backend is not running" } as T };
    return { status: Number(answer.head.status), data: JSON.parse(answer.body.toString("utf8")) as T };
  }

  /**
   * Close stdin and wait for the app's shutdown. Killed if it has not exited
   * within `patience` milliseconds; the WAL is then replayed at the next
   * start, so nothing is lost, only the tidy exit.
   */
  async stop(patience = 30_000): Promise<number | null> {
    if (this.running) this.child.stdin.end();
    const timer = setTimeout(() => this.child.kill(), patience);
    try {
      return await this.exited;
    } finally {
      clearTimeout(timer);
    }
  }
}
