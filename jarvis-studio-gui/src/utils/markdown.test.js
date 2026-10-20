import { describe, expect, it } from "vitest";
import { inlineMarkdownSegments, plainPreview, splitMarkdownTables } from "./markdown";

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

describe("plainPreview", () => {
  it("flattens a reply to one readable line", () => {
    const reply = "# Forecast\n**29°C** and `partly cloudy`, see [the map](https://x.y).\n"
      + "| Day | High |\n|---|---|\n| Mon | 31 |\n```json\n{\"a\": 1}\n```\n- 2*3 in my_file.txt";
    expect(plainPreview(reply)).toBe("Forecast 29°C and partly cloudy, see the map. 2*3 in my_file.txt");
  });

  it("is empty for a reply that is only a chart or table", () => {
    expect(plainPreview("```chart\n{}\n```")).toBe("");
    expect(plainPreview("| a |\n|---|")).toBe("");
    expect(plainPreview(undefined)).toBe("");
  });
});
