import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { LogForge } from "../sdk/javascript/index.js";

function setup(options = {}) {
  const events = [];
  LogForge.reset();
  LogForge.configure({ tags: [], level: "debug", enabled: true, runId: "unit-run", sink: event => events.push(event), ...options });
  return events;
}
test("feature tags are case insensitive and filtered payloads never execute", () => {
  const events = setup({ tags: [" CHECKOUT "] });
  let evaluated = 0;
  LogForge.out("catalog", () => { throw new Error("must not run"); });
  LogForge.out("Checkout", () => { evaluated++; return { ok: true }; }, { tags: ["BACKEND"] });
  assert.equal(evaluated, 1);
  assert.equal(events.length, 1);
  assert.deepEqual(events[0].tags, ["checkout", "backend"]);
  assert.equal(LogForge.stats().filtered, 1);
  assert.equal(LogForge.stats().errors, 0);
});
test("no selection enables every tag; multiple filters use OR", () => {
  const events = setup();
  LogForge.out("first", 1); LogForge.out("second", 2);
  assert.equal(events.length, 2);
  LogForge.configure({ tags: ["frontend", "database"] });
  LogForge.out("checkout", 3, { tags: ["DATABASE"] });
  assert.equal(events.length, 3);
});
test("minimum level is checked before payload evaluation", () => {
  const events = setup({ level: "warn" });
  LogForge.debug("checkout", () => { throw new Error("filtered"); });
  LogForge.warn("checkout", { timeout: true });
  assert.equal(events.length, 1);
  assert.equal(events[0].level, "warn");
  assert.equal(LogForge.stats().errors, 0);
});
test("structured secrets and circular values are safely serialized", () => {
  const events = setup();
  const value = { password: "never-print-this", nested: { apiKey: "hidden" }, count: 9n };
  value.self = value;
  LogForge.out("checkout", () => value);
  assert.equal(events[0].data.password, "[REDACTED]");
  assert.equal(events[0].data.nested.apiKey, "[REDACTED]");
  assert.equal(events[0].data.count, "9");
  assert.equal(events[0].data.self, "[circular]");
  assert.ok(!JSON.stringify(events).includes("never-print-this"));
});
test("source points at application code and named checks record true or false", () => {
  const events = setup();
  LogForge.check("checkout", "positive", () => true);
  LogForge.check("checkout", "negative", () => false);
  assert.equal(events[0].passed, true);
  assert.equal(events[1].passed, false);
  assert.equal(events[0].source.file, fileURLToPath(import.meta.url).replaceAll("\\", "/"));
  assert.ok(events[0].source.line > 0);
});
test("diagnostic and sink failures never throw into business code", () => {
  const events = setup();
  assert.doesNotThrow(() => LogForge.out("checkout", () => { throw new Error("diagnostic failure"); }));
  assert.equal(events[0].data.diagnostic_error.message, "diagnostic failure");
  LogForge.configure({ sink: () => { throw new Error("offline"); } });
  assert.doesNotThrow(() => LogForge.out("checkout", 1));
  assert.equal(LogForge.stats().errors, 2);
});
test("flush waits for browser transport and produces a summary", async () => {
  const events = setup({ sink: async event => { await new Promise(resolve => setTimeout(resolve, 10)); events.push(event); } });
  LogForge.out("checkout", 1);
  await LogForge.flush();
  assert.equal(events.length, 2);
  assert.equal(events[1]._logforge, "summary");
  assert.equal(events[1].stats.emitted, 1);
});
