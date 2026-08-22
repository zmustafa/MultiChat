import { describe, expect, it } from "vitest";
import { splitStreamingMarkdown } from "./MessageRenderer";

const long = (s: string) => s.padEnd(1600, " ");

describe("splitStreamingMarkdown", () => {
  it("keeps short content whole — splitting is not worth it", () => {
    const content = "# Title\n\nsome text";
    expect(splitStreamingMarkdown(content)).toEqual({ stable: "", tail: content });
  });

  it("splits at the last completed block", () => {
    const content = long("# Title\n\nparagraph one\n\n") + "\n\nparagraph two";
    const { stable, tail } = splitStreamingMarkdown(content);
    expect(stable + tail).toBe(content);
    expect(stable).toContain("paragraph one");
    expect(tail).toContain("paragraph two");
  });

  it("never cuts inside an unterminated code fence", () => {
    const content =
      long("intro paragraph\n\n") +
      "\n\n```python\ndef f():\n\n    return 1\n\n    # still writing";
    const { stable, tail } = splitStreamingMarkdown(content);
    expect(stable + tail).toBe(content);
    // The half-written block must be entirely in the tail, fence included.
    expect(stable).not.toContain("```");
    expect(tail.trimStart().startsWith("```python")).toBe(true);
  });

  it("may cut after a closed code fence", () => {
    const content =
      long("intro\n\n") + "\n\n```js\nconst a = 1;\n```\n\nafter the block";
    const { stable, tail } = splitStreamingMarkdown(content);
    expect(stable + tail).toBe(content);
    expect(stable).toContain("```js");
    expect(tail).toContain("after the block");
  });

  it("is lossless for every prefix of a streamed answer", () => {
    const full =
      "# Heading\n\nfirst para\n\n```ts\nconst x: number = 1;\n```\n\n" +
      "| a | b |\n| - | - |\n| 1 | 2 |\n\n".repeat(40) +
      "closing words";
    for (let i = 0; i <= full.length; i += 37) {
      const chunk = full.slice(0, i);
      const { stable, tail } = splitStreamingMarkdown(chunk);
      expect(stable + tail).toBe(chunk);
    }
  });

  it("returns everything as tail when there is no block boundary", () => {
    const content = "x".repeat(2000);
    expect(splitStreamingMarkdown(content)).toEqual({ stable: "", tail: content });
  });

  it("keeps a markdown table in one piece", () => {
    const table = "| a | b |\n| - | - |\n| 1 | 2 |";
    const content = long("intro\n\n") + "\n\n" + table;
    const { stable, tail } = splitStreamingMarkdown(content);
    expect(stable).not.toContain("|");
    expect(tail).toContain(table);
  });
});
