import { copyFileSync, mkdirSync, readdirSync, readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
const target = resolve("public/gesture-assets");
const model = readFileSync(resolve(target, "gesture_recognizer.task"));
const sha = createHash("sha256").update(model).digest("hex");
if (sha !== "97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482")
  throw new Error("Gesture model checksum mismatch.");
const source = resolve("node_modules/@mediapipe/tasks-vision");
mkdirSync(resolve(target, "wasm"), { recursive: true });
copyFileSync(
  resolve(source, "vision_bundle.js"),
  resolve(target, "vision_bundle.js"),
);
for (const file of readdirSync(resolve(source, "wasm"))) {
  if (file.endsWith(".js") || file.endsWith(".wasm"))
    copyFileSync(resolve(source, "wasm", file), resolve(target, "wasm", file));
}
