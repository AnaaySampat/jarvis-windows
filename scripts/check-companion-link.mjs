/** Real TypeScript ↔ Python WebSocket/crypto regression. Node 22+ required.
 * node scripts/check-companion-link.mjs ../jarvis /path/to/backend/python
 * Uses fresh synthetic identities/data. No user credentials or devices needed.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash, createPublicKey, generateKeyPairSync, sign, verify } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, resolve, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const android = resolve(process.argv[2] || join(root, "../jarvis"));
const python = process.argv[3] || "python";
const requireAndroid = createRequire(join(android, "jarvis-studio-gui/package.json"));
const { build } = requireAndroid("esbuild");
const directory = await mkdtemp(join(tmpdir(), "jarvis-client-probe-"));
let child;
let pc;

async function until(check, description, timeout = 8_000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (check()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error(`Timed out: ${description}`);
}

try {
  const output = join(directory, "pc.mjs");
  await build({
    entryPoints: [join(android, "jarvis-studio-gui/src/brain/remote/pc.ts")],
    outfile: output, bundle: true, platform: "node", format: "esm", logLevel: "silent",
  });
  const { RemotePC } = await import(pathToFileURL(output).href);
  child = spawn(python, [join(root, "scripts/companion_link_fixture.py"), directory], {
    cwd: root, stdio: ["ignore", "pipe", "pipe"],
  });
  let setup;
  let fixtureError = "";
  child.stderr.on("data", (data) => { fixtureError += data.toString(); });
  child.on("error", (error) => { fixtureError += error.message; });
  const lines = createInterface({ input: child.stdout });
  lines.on("line", (line) => {
    if (line.startsWith("COMPANION_PROBE ")) setup = JSON.parse(line.slice(16));
  });
  await until(() => setup || fixtureError, "Python fixture startup");
  if (!setup) throw new Error(fixtureError);

  const { pairing, port } = setup;
  const { privateKey, publicKey } = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const publicDer = publicKey.export({ type: "spki", format: "der" });
  const publicB64 = publicDer.toString("base64url");
  const deviceId = "phone-" + createHash("sha256").update(publicDer).digest("hex").slice(0, 32);
  const fingerprint = "sha256:" + createHash("sha256").update(publicDer).digest("base64url");
  let counter = 0;
  let pairings = 0;
  const signed = (material) => sign("sha256", Buffer.from(material), privateKey).toString("base64url");
  const verifyHost = (input, material) => {
    const key = createPublicKey({ key: Buffer.from(input.publicKey, "base64url"), type: "spki", format: "der" });
    const actual = "sha256:" + createHash("sha256").update(key.export({ type: "spki", format: "der" })).digest("base64url");
    return { ok: true, verified: actual === input.expectedFingerprint &&
      verify("sha256", Buffer.from(material), key, Buffer.from(input.signature, "base64url")) };
  };
  const identityBridge = {
    info: async () => ({ ok: true, deviceId, publicKey: publicB64, fingerprint }),
    signAuth: async ({ hostId, nonce }) => {
      const n = ++counter;
      return { ok: true, deviceId, counter: n,
        signature: signed(`aura-auth-v1\n${hostId}\n${deviceId}\n${nonce}\n${n}`) };
    },
    signPairing: async ({ hostId, challengeId, connectionNonce, deviceName }) => {
      pairings++;
      return { ok: true, deviceId, publicKey: publicB64, fingerprint, deviceName,
        signature: signed(`aura-pair-v1\n${hostId}\n${challengeId}\n${deviceId}\n${connectionNonce}\n${publicB64}\n${deviceName}`) };
    },
    signEnvelope: async ({ connectionNonce, messageType, taskId, payloadDigest }) => {
      const n = ++counter;
      return { ok: true, deviceId, counter: n,
        signature: signed(`aura-envelope-v1\ndevice\n${deviceId}\n${connectionNonce}\n${n}\n${messageType}\n${taskId}\n${payloadDigest}`) };
    },
    verifyHostChallenge: async (input) => verifyHost(input,
      `aura-host-challenge-v1\n${input.hostId}\n${input.nonce}\n${input.expiresAt}\n${input.connectionId}`),
    verifyHostEnvelope: async (input) => verifyHost(input,
      `aura-envelope-v1\nhost\n${input.hostId}\n${input.connectionNonce}\n${input.counter}\n${input.messageType}\n${input.taskId}\n${input.payloadDigest}`),
  };
  const events = [];
  const states = [];
  let droppedDuringApproval = false;
  pc = new RemotePC({
    host: "127.0.0.1", port, hostId: pairing.host_id,
    hostFingerprint: pairing.host_fingerprint, hostPublicKey: pairing.host_public_key,
    pairingChallengeId: pairing.challenge_id, pin: pairing.pin, deviceName: "Synthetic phone",
    identityBridge, reconnectMs: 20,
    onStateChange: (state) => states.push(state),
    onEvent: (event) => {
      events.push(event);
      if (event.event === "phone.command") {
        void pc.signalConfirmed("phone.result", { id: event.data.id, ok: true, reply: "Synthetic result" });
      }
      if (event.event === "task.event" && event.data.kind === "approval.challenge") {
        if (!droppedDuringApproval) {
          droppedDuringApproval = true;
          pc.nudge(true); // lose the dialog after its cursor has already advanced
        } else {
          assert.equal(pc.respondPermission(event.data.payload.step_id, true), true);
        }
      }
    },
  });
  pc.connect();
  await until(() => pc.isOnline, "signed QR/PIN pairing");
  assert.equal(pairings, 1);
  console.log("PASS: real P-256 QR/PIN pairing and pinned host verification");

  const data = { tiny: 1e-7, decimal: 1e-6, large: 1e21, integer: 1e20,
    nested: [{ "\ue000": "private-use", "🚀": "non-BMP", value: -0.0 }] };
  assert.equal(await pc.signalConfirmed("probe.echo", data), true);
  await until(() => events.some((event) => event.event === "probe.echoed"), "bidirectional canonical signatures");
  assert.deepEqual(events.find((event) => event.event === "probe.echoed").data, JSON.parse(JSON.stringify(data)));
  console.log("PASS: signed payloads in both directions, including number/Unicode boundaries");

  await pc.signalConfirmed("memory.sync", { request_id: "synthetic-memory-1", since: 0,
    facts: [{ key: "synthetic fact", text: "Synthetic fact", ts: Date.now() }], routines: [] });
  await until(() => events.some((event) => event.event === "memory.synced"), "memory sync");
  assert.equal(events.find((event) => event.event === "memory.synced").data.facts[0].text, "Synthetic fact");
  assert.equal(events.find((event) => event.event === "memory.synced").data.request_id, "synthetic-memory-1");
  console.log("PASS: memory exchange through the production merge");

  await pc.signalConfirmed("probe.phone", {});
  await until(() => events.some((event) => event.event === "probe.phone_result"), "PC-to-phone result correlation");
  assert.equal(events.find((event) => event.event === "probe.phone_result").data.ok, true);
  console.log("PASS: PC-to-phone command and signed correlated reply");

  const result = await pc.runTask("Synthetic transport task");
  assert.equal(result.ok, true);
  assert.ok(states.filter((state) => state === "online").length >= 2);
  assert.equal(pairings, 1);
  console.log("PASS: authenticated reconnect and task completion without re-pairing");

  const denied = await pc.runTask("take a screenshot", "auto", "incorrect-test-passkey");
  assert.equal(denied.ok, false);
  assert.equal(denied.error, "passkey_incorrect");
  assert.equal(events.some((event) => event.data?.kind === "approval.challenge"), false);
  console.log("PASS: the laptop rejects an incorrect task passkey before approval or capture");
  const screenshot = await pc.runTask("send me a screenshot of my laptop", "auto", "test-only-companion-passkey");
  assert.equal(screenshot.ok, true);
  assert.equal(screenshot.data.artifacts[0].data_url, "data:image/png;base64,c3ludGhldGlj");
  assert.ok(events.some((event) => event.data?.kind === "approval.challenge"));
  assert.ok(states.filter((state) => state === "online").length >= 3);
  assert.equal(events.some((event) => event.event === "permission_request"), false);
  console.log("PASS: screenshot approval returns to the phone after reconnect and only then releases its image");

  await pc.signalConfirmed("probe.privacy", {});
  await until(() => events.some((event) => event.event === "probe.privacy_done"), "privacy check");
  assert.equal(events.some((event) => event.event === "config"), false);
  assert.deepEqual(Object.values(events.find((event) => event.event === "probe.privacy_done").data.submissions), [1]);
  console.log("PASS: desktop broadcasts stay private and reconnect does not duplicate task submission");

  await pc.signalConfirmed("probe.revoke", {});
  await until(() => pc.state === "unauthorized", "immediate device revocation");
  assert.equal(await pc.signalConfirmed("probe.echo", {}), false);
  console.log("PASS: revocation terminates access immediately");
} finally {
  pc?.close();
  if (child?.pid && child.exitCode === null) {
    const exited = new Promise((resolve) => child.once("exit", resolve));
    child.kill("SIGINT");
    await exited;
  }
  await rm(directory, { recursive: true, force: true });
}
