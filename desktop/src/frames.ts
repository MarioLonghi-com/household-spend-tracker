/**
 * The shell's half of the wire format in `app/desktop/stdio_adapter.py` (#319).
 *
 * Frames of a 4-byte big-endian length and that many bytes. A request or an
 * answer is two frames: a JSON head, then the body. Heads carry an `id`, so
 * answers can arrive in any order; id 0 is the backend talking about itself
 * (`upgrading`, `ready`, `stopped`, `restored`).
 */

export type Head = { id: number; [key: string]: unknown };
export type Message = { head: Head; body: Buffer };

export function encode(head: Head, body: Buffer = Buffer.alloc(0)): Buffer {
  const raw = Buffer.from(JSON.stringify(head), "utf8");
  const lengths = Buffer.alloc(8);
  lengths.writeUInt32BE(raw.length, 0);
  lengths.writeUInt32BE(body.length, 4);
  return Buffer.concat([lengths.subarray(0, 4), raw, lengths.subarray(4), body]);
}

/** Collects bytes as they arrive and hands back each complete message. */
export class FrameReader {
  private buffer: Buffer = Buffer.alloc(0);
  private head: Head | null = null;

  push(chunk: Buffer): Message[] {
    this.buffer = this.buffer.length ? Buffer.concat([this.buffer, chunk]) : chunk;
    const done: Message[] = [];
    for (;;) {
      if (this.buffer.length < 4) break;
      const length = this.buffer.readUInt32BE(0);
      if (this.buffer.length < 4 + length) break;
      // A copy: the buffer behind a subarray is reused by the next chunk.
      const frame = Buffer.from(this.buffer.subarray(4, 4 + length));
      this.buffer = this.buffer.subarray(4 + length);
      if (this.head === null) {
        this.head = JSON.parse(frame.toString("utf8")) as Head;
      } else {
        done.push({ head: this.head, body: frame });
        this.head = null;
      }
    }
    return done;
  }
}
