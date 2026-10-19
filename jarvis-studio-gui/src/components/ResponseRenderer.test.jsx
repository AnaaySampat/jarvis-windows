import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import ResponseRenderer from "./ResponseRenderer";

describe("ResponseRenderer text", () => {
  it("draws **bold** and a pipe table instead of raw punctuation", () => {
    const html = renderToStaticMarkup(
      <ResponseRenderer text={"The **cheapest**:\n| Fruit | Price |\n|---|---|\n| Apple | $2 |"} />,
    );
    expect(html).toContain("<strong>cheapest</strong>");
    expect(html).toContain("<th>Fruit</th>");
    expect(html).toContain("<td>$2</td>");
    expect(html).not.toContain("|");
  });
});
