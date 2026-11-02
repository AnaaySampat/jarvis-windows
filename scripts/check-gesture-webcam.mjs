/** Bundled MediaPipe + real browser camera lifecycle smoke test.
 * Node 22+ and Chromium required. Uses a synthetic camera, no user images.
 * Run: node scripts/check-gesture-webcam.mjs
 * Set CHROMIUM_PATH when Chromium is not on PATH.
 */
import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const gui = join(root, "jarvis-studio-gui");
const profile = await mkdtemp(join(tmpdir(), "jarvis-gesture-smoke-"));
const children = [];
let socket;
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(check, description, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const result = await check();
    if (result) return result;
    await delay(50);
  }
  throw new Error(`Timed out: ${description}`);
}
function launch(command, args) {
  const child = spawn(command, args, { cwd: gui, stdio: ["ignore", "pipe", "pipe"] });
  let log = "";
  child.stdout.on("data", (data) => { log = (log + data).slice(-4000); });
  child.stderr.on("data", (data) => { log = (log + data).slice(-4000); });
  child.on("error", (error) => { log += error.message; });
  children.push(child);
  return () => {
    if (child.exitCode !== null || !child.pid) throw new Error(log || "Could not start process");
  };
}
try {
  await promisify(execFile)(process.execPath, [join(gui, "scripts/prepare-gestures.mjs")], { cwd: gui });
  const server = createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  const origin = `http://127.0.0.1:${port}`;
  const checkVite = launch(process.execPath, [
    join(root, "jarvis-studio-gui/node_modules/vite/bin/vite.js"),
    "--host", "127.0.0.1", "--port", String(port), "--strictPort",
  ]);
  await until(async () => {
    checkVite();
    try { return (await fetch(origin)).ok; } catch { return false; }
  }, "Vite startup");
  const checkChrome = launch(process.env.CHROMIUM_PATH || "chromium", [
    "--headless", "--no-sandbox", "--disable-dev-shm-usage",
    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required", "--remote-debugging-port=0",
    `--user-data-dir=${profile}`, "about:blank",
  ]);
  const debugPort = await until(async () => {
    checkChrome();
    try { return Number((await readFile(join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0]); }
    catch { return false; }
  }, "Chromium startup");
  const pages = await (await fetch(`http://127.0.0.1:${debugPort}/json`)).json();
  socket = new WebSocket(pages.find((page) => page.type === "page").webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  const pending = new Map();
  let id = 0;
  socket.addEventListener("message", ({ data }) => {
    const response = JSON.parse(data);
    const request = pending.get(response.id);
    if (!request) return;
    pending.delete(response.id);
    clearTimeout(request.timer);
    if (response.error) request.reject(new Error(JSON.stringify(response.error)));
    else request.resolve(response.result);
  });
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const token = ++id;
    const timer = setTimeout(() => {
      pending.delete(token);
      reject(new Error(`Browser command timed out: ${method}`));
    }, 60000);
    pending.set(token, { resolve, reject, timer });
    socket.send(JSON.stringify({ id: token, method, params }));
  });
  await call("Page.navigate", { url: origin });
  await until(async () => (await call("Runtime.evaluate", {
    expression: `location.origin === ${JSON.stringify(origin)} && document.readyState === 'complete'`,
    returnByValue: true,
  })).result.value, "page load");
  const result = await call("Runtime.evaluate", {
    awaitPromise: true, returnByValue: true,
    expression: `(async () => {
      const { WebcamCamera } = await import('/src/gestures/webcam.ts');
      const { defaults } = await import('/src/gestures/engine.ts');
      const camera = new WebcamCamera();
      const wait = ms => new Promise(r => setTimeout(r, ms));
      const firstFrame = async () => {
        const end = performance.now() + 10000;
        while (performance.now() < end) {
          const state = await camera.poll();
          if (state.running && state.frame) return state.frame;
          if (!state.starting) throw new Error(state.summary);
          await wait(20);
        }
        throw new Error('No camera frame arrived');
      };
      try {
        const started = await camera.start(defaults());
        if (!started.ok) throw new Error(started.summary);
        await firstFrame();
        const frames = new Map();
        const began = performance.now();
        while (performance.now() - began < 3000) {
          const state = await camera.poll();
          if (!state.running) throw new Error(state.summary);
          if (!frames.has(state.frame.seq)) frames.set(state.frame.seq, Date.now() - state.frame.observedAt);
          await wait(20);
        }
        const tracks = camera.stream.getTracks();
        const previousSeq = (await camera.poll()).frame.seq;
        await camera.stop();
        if ((await camera.poll()).running || camera.stream || camera.worker || camera.video ||
            tracks.some(t => t.readyState !== 'ended')) throw new Error('Camera resources retained after stop');
        const restart = await camera.start(defaults());
        if (!restart.ok) throw new Error(restart.summary);
        if ((await firstFrame()).seq <= previousSeq) throw new Error('Restart returned an old frame');
        const ages = [...frames.values()].sort((a, b) => a - b);
        return { frames: frames.size, sampleMs: 3000, medianFrameAgeMs: ages[Math.floor(ages.length / 2)], restart: true, cleanup: true };
      } finally { await camera.stop(); }
    })()`,
  });
  assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
  const report = result.result.value;
  assert.ok(report.frames >= 5, `Camera made no sustained progress: ${JSON.stringify(report)}`);
  assert.equal(report.restart, true);
  assert.equal(report.cleanup, true);
  console.log("PASS: bundled gesture worker, synthetic webcam, restart and cleanup");
  console.log(JSON.stringify(report, null, 2));
} finally {
  socket?.close();
  await Promise.all(children.map(async (child) => {
    if (!child.pid || child.exitCode !== null) return;
    const exited = new Promise((resolve) => child.once("exit", resolve));
    child.kill("SIGTERM");
    await Promise.race([exited, delay(3000)]);
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGKILL");
      await exited;
    }
  }));
  await rm(profile, { recursive: true, force: true, maxRetries: 3 });
}
