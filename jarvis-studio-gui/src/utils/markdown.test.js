import { describe, expect, it } from "vitest";
import { inlineMarkdownSegments, splitMarkdownTables } from "./markdown";

describe("inlineMarkdownSegments", () => {
  it("splits bold and bold italic, leaving single asterisks alone", () => {
    expect(inlineMarkdownSegments("a **b** c ***d*** 2*3*4")).toEqual([
      { kind: "text", content: "a " },
      { kind: "bold", content: "b" },
      { kind: "text", content: " c " },
      { kind: "boldItalic", content: "d" },
      { kind: "text", content: " 2*3*4" },
    ]);
  });

  it("returns plain text untouched", () => {
    expect(inlineMarkdownSegments("no marks")).toEqual([{ kind: "text", content: "no marks" }]);
  });
});

describe("splitMarkdownTables", () => {
  it("pulls a table out from between prose", () => {
    const text = "Here you go:\n| Name | Price |\n|---|:--:|\n| Apple | $2 |\n| Pear |\nEnjoy.";
    expect(splitMarkdownTables(text)).toEqual([
      { kind: "text", content: "Here you go:" },
      { kind: "table", header: ["Name", "Price"], rows: [["Apple", "$2"], ["Pear", ""]] },
      { kind: "text", content: "Enjoy." },
    ]);
  });

  it("keeps an escaped pipe inside a cell and ignores pipes without a divider", () => {
    expect(splitMarkdownTables("| a \\| b | c |\n| - | - |\n| 1 | 2 |")[0].header).toEqual([
      "a | b",
      "c",
    ]);
    expect(splitMarkdownTables("x | y")).toEqual([{ kind: "text", content: "x | y" }]);
  });
});
