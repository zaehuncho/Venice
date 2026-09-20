/**
 * Probe harness for discord_launch/orion_worker.js, driven by
 * tests/discord/test_orion_worker_purchase.py.
 *
 * The Worker is ESM with no test runner in this repo, so pytest shells out to
 * node with this file: it imports the real module, runs ONE named probe with a
 * fake `env` + a stubbed `fetch`, and prints the result as JSON on stdout. No
 * network: every /api/bot/* call is answered from the canned reply passed in.
 *
 * Usage:  node worker_probe.mjs '<json>'
 *   { probe: "purchase"|"hwidReset"|"constants", env: {...},
 *     lambdaReply: {...}, data: {...}, uid: "…" }
 */
import { pathToFileURL } from "node:url";
import path from "node:path";

const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"));
const WORKER = pathToFileURL(path.join(HERE, "..", "..", "discord_launch", "orion_worker.js"));

const input = JSON.parse(process.argv[2] || "{}");

// Every network call the Worker makes goes through fetch(); answer it from the
// canned reply so the probe is hermetic.
globalThis.fetch = async () => ({
  ok: true,
  status: 200,
  json: async () => input.lambdaReply ?? {},
});

const mod = await import(WORKER.href);

const interaction = {
  data: { name: input.command || "purchase", options: input.options || [] },
  member: { user: { id: input.uid || "1000" } },
  user: { id: input.uid || "1000" },
};

let out;
switch (input.probe) {
  case "purchase":
    out = await mod.runCommand(input.env || {}, interaction);
    break;
  case "hwidReset":
    out = mod.renderHwidReset(input.env || {}, input.data || {}, input.uid || "1000");
    break;
  case "constants":
    out = {
      commandNames: mod.COMMAND_NAMES,
      subscribeLabel: mod.SUBSCRIBE_LABEL,
      exports: Object.keys(mod).sort(),
      storeUrl: mod.storeUrl(input.env || {}),
      deductDays: mod.deductDays(input.data || {}),
    };
    break;
  default:
    throw new Error(`unknown probe: ${input.probe}`);
}
process.stdout.write(JSON.stringify(out));
