import { describe, expect, it } from "vitest";
import { encode, FrameReader } from "../src/frames";

describe("the frames (#319)", () => {
  it("a message split anywhere comes back whole, accents and all", () => {
    const body = Buffer.from("Café Ñandú — Göteborg", "utf8");
    const wire = Buffer.concat([encode({ id: 7, status: 200 }, body), encode({ id: 8, status: 204 })]);
    for (let cut = 1; cut < wire.length; cut += 1) {
      const reader = new FrameReader();
      const got = [...reader.push(wire.subarray(0, cut)), ...reader.push(wire.subarray(cut))];
      expect(got.map((m) => [m.head.id, m.body.toString("utf8")])).toEqual([
        [7, "Café Ñandú — Göteborg"],
        [8, ""],
      ]);
    }
  });

  it("is the length-prefixed layout the Python side reads", () => {
    const wire = encode({ id: 1 }, Buffer.from("ab"));
    const head = Buffer.from(JSON.stringify({ id: 1 }));
    expect(wire.readUInt32BE(0)).toBe(head.length);
    expect(wire.subarray(4, 4 + head.length).toString()).toBe('{"id":1}');
    expect(wire.readUInt32BE(4 + head.length)).toBe(2);
    expect(wire.subarray(8 + head.length).toString()).toBe("ab");
  });

  it("does not hand back a body whose bytes a later chunk reuses", () => {
    const reader = new FrameReader();
    const chunk = Buffer.from(encode({ id: 3 }, Buffer.from("first")));
    const [message] = reader.push(chunk);
    chunk.fill(0);
    expect(message.body.toString()).toBe("first");
  });
});
