// The small, safe Markdown subset the chat renders: **bold** / ***bold italic*** inline,
// and GitHub-style pipe tables. Models write both unprompted, and shown raw they were
// asterisks and rows of pipes. No HTML is ever interpreted. Port of the Android app's
// brain/formatting.ts; the backend's tts/speech_text.py says the same text aloud.

/** Split text into plain / bold / boldItalic segments. */
export function inlineMarkdownSegments(text) {
  const source = text ?? "";
  const out = [];
  // Longest marker first, so ***x*** isn't read as ** + *x.
  const emphasis = /(\*\*\*|___|\*\*|__)(?=\S)([\s\S]*?\S)\1/g;
  let cursor = 0;
  let match;
  while ((match = emphasis.exec(source)) !== null) {
    if (match.index > cursor) out.push({ kind: "text", content: source.slice(cursor, match.index) });
    out.push({ kind: match[1].length === 3 ? "boldItalic" : "bold", content: match[2] });
    cursor = match.index + match[0].length;
  }
  if (cursor < source.length) out.push({ kind: "text", content: source.slice(cursor) });
  return out.length ? out : [{ kind: "text", content: source }];
}

const TABLE_DIVIDER = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;

/** One pipe-table row's cells; `\|` is a literal pipe inside a cell. */
function tableCells(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/(?<!\\)\|$/, "")
    .split(/(?<!\\)\|/)
    .map((c) => c.trim().replace(/\\\|/g, "|"));
}

/** Split pipe tables (header row, `|---|:--:|` divider, body rows) out of a reply:
 *  [{kind:"text", content}] and [{kind:"table", header, rows}] blocks, in order. */
export function splitMarkdownTables(text) {
  const lines = (text ?? "").split("\n");
  const out = [];
  let prose = [];
  const flush = () => {
    const content = prose.join("\n").trim();
    if (content) out.push({ kind: "text", content });
    prose = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const divider = lines[i + 1];
    if (!line.includes("|") || divider === undefined || !divider.includes("|") || !TABLE_DIVIDER.test(divider)) {
      prose.push(line);
      continue;
    }
    const header = tableCells(line);
    const rows = [];
    for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim(); i++) {
      // Every row as wide as the header, so a short row doesn't shift its neighbours.
      const cells = tableCells(lines[i]);
      rows.push(header.map((_, c) => cells[c] ?? ""));
    }
    i--;
    flush();
    out.push({ kind: "table", header, rows });
  }
  flush();
  return out.length ? out : [{ kind: "text", content: text ?? "" }];
}

/** A reply as one plain line, for the home screen's preview card: no code blocks,
 *  table rows, list/heading/quote markers, link targets or emphasis marks. Single
 *  asterisks and underscores (2*3, my_file.txt) are text and stay. */
export function plainPreview(text) {
  const flat = (text ?? "")
    .replace(/```[\s\S]*?(```|$)/g, " ")
    .split("\n")
    .filter((line) => !/^\s*\|/.test(line))
    .map((line) => line.replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+/, ""))
    .join(" ")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/`([^`]*)`/g, "$1");
  return inlineMarkdownSegments(flat).map((s) => s.content).join("").replace(/\s+/g, " ").trim();
}
