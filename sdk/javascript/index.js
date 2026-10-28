/** LogForge.out: filter before evaluating diagnostic payloads. No runtime dependencies. */
const node = typeof process !== "undefined" && !!process.versions?.node;
const env = node ? process.env : {};
const ranks = { debug: 10, info: 20, warn: 30, error: 40 };
const secretKey = /password|passwd|secret|token|authorization|cookie|api.?key/i;
let sequence = 0;
let settings = {
  tags: split(env.LOGFORGE_TAGS), level: env.LOGFORGE_LEVEL || "debug",
  runId: env.LOGFORGE_RUN_ID || null, service: env.LOGFORGE_SERVICE || "application",
  sink: defaultSink, enabled: env.LOGFORGE_ENABLED !== "false", clock: () => new Date().toISOString(),
};
let counts = { emitted: 0, filtered: 0, errors: 0, sites: Object.create(null) };
let pending = new Set();
let summaryWritten = false;

function normalize(tags) { return [...new Set(tags.map(x => String(x).trim().toLowerCase()).filter(Boolean))]; }
function split(value) { return value ? normalize(String(value).split(",")) : []; }
function defaultSink(event) {
  const line = JSON.stringify(event);
  if (node) process.stdout.write(line + "\n");
  else console.log(line);
}
function clean(value, seen = new WeakSet(), depth = 0) {
  if (depth > 8) return "[depth limited]";
  if (typeof value === "bigint") return String(value);
  if (value === undefined) return null;
  if (typeof value === "number" && !Number.isFinite(value)) return String(value);
  if (typeof value === "string") return value.length > 8192 ? value.slice(0, 8192) + "[truncated]" : value;
  if (value === null || typeof value !== "object") return typeof value === "function" ? "[function]" : value;
  if (seen.has(value)) return "[circular]";
  seen.add(value);
  let result;
  if (value instanceof Error) result = { name: value.name, message: clean(value.message), stack: clean(value.stack) };
  else if (Array.isArray(value)) result = value.slice(0, 100).map(v => clean(v, seen, depth + 1));
  else result = Object.fromEntries(Object.keys(value).slice(0, 100).map(k => [k, secretKey.test(k) ? "[REDACTED]" : clean(value[k], seen, depth + 1)]));
  seen.delete(value);
  return result;
}
function sourceLocation() {
  const lines = (new Error().stack || "").split("\n").slice(1);
  for (const line of lines) {
    if (line.includes(import.meta.url) || line.includes("node:internal")) continue;
    const match = line.match(/\((.+):(\d+):(\d+)\)$/) || line.match(/^\s*at\s+(.+):(\d+):(\d+)$/) || line.match(/@(.+):(\d+):(\d+)$/);
    if (!match) continue;
    let file = match[1];
    if (file.startsWith("file://")) {
      file = decodeURIComponent(new URL(file).pathname);
      if (/^\/[A-Za-z]:/.test(file)) file = file.slice(1);
    }
    const root = env.LOGFORGE_PROJECT_ROOT?.replaceAll("\\", "/").replace(/\/$/, "");
    file = file.replaceAll("\\", "/");
    if (root && file.startsWith(root + "/")) file = file.slice(root.length + 1);
    return { file, line: Number(match[2]), column: Number(match[3]), language: "javascript" };
  }
  return null;
}
function deliver(event) {
  try {
    if (JSON.stringify(event).length > 65536) event.data = { truncated: true, reason: "payload exceeds 64 KiB" };
    const result = settings.sink(event);
    if (result && typeof result.then === "function") {
      const promise = Promise.resolve(result).catch(() => { counts.errors++; }).finally(() => pending.delete(promise));
      pending.add(promise);
    }
  } catch { counts.errors++; }
}
function selected(tags, level) {
  return settings.enabled && ranks[level] >= ranks[settings.level] &&
    (!settings.tags.length || settings.tags.includes("*") || normalize(tags).some(t => settings.tags.includes(t)));
}
function emit(tag, payload, options = {}, kind = "output") {
  summaryWritten = false;
  if (typeof tag !== "string") { options = payload || {}; payload = tag; tag = "app"; }
  const tags = normalize([tag, ...(options.tags || [])]);
  const level = options.level || "debug";
  const name = options.name || tag;
  const key = options.id || name;
  const site = counts.sites[key] ||= { name, tags, emitted: 0, filtered: 0 };
  if (!selected(tags, level)) { counts.filtered++; site.filtered++; return undefined; }
  let data;
  try { data = clean(typeof payload === "function" ? payload() : payload); }
  catch (error) { counts.errors++; data = { diagnostic_error: clean(error) }; }
  const event = {
    _logforge: "event", schema_version: "1.0", stream: "logforge.out",
    event_id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${++sequence}-${Math.random().toString(16).slice(2)}`,
    run_id: settings.runId, timestamp: settings.clock(), service: settings.service,
    kind, name, site_id: options.id || null, tags, level, source: options.source || sourceLocation(), data,
  };
  if (kind === "check") event.passed = data === true;
  counts.emitted++; site.emitted++;
  deliver(event);
  return event;
}
function summary() {
  return { _logforge: "summary", schema_version: "1.0", run_id: settings.runId,
    service: settings.service, timestamp: settings.clock(), stats: structuredClone(counts) };
}
export const LogForge = {
  configure(options = {}) {
    if (options.level !== undefined && !(options.level in ranks)) throw new TypeError("Unknown LogForge level");
    if (options.tags !== undefined && (!Array.isArray(options.tags) || options.tags.some(t => typeof t !== "string"))) throw new TypeError("tags must be a string array");
    settings = { ...settings, ...options, tags: options.tags ? normalize(options.tags) : settings.tags };
    return this;
  },
  out: (tag, payload, options) => emit(tag, payload, options),
  debug: (tag, payload, options) => emit(tag, payload, { ...options, level: "debug" }),
  info: (tag, payload, options) => emit(tag, payload, { ...options, level: "info" }),
  warn: (tag, payload, options) => emit(tag, payload, { ...options, level: "warn" }),
  error: (tag, payload, options) => emit(tag, payload, { ...options, level: "error" }),
  check: (tag, name, predicate, options) => emit(tag, predicate, { ...options, name, level: options?.level || "info" }, "check"),
  isEnabled: (tag, level = "debug") => selected(Array.isArray(tag) ? tag : [tag], level),
  stats: () => structuredClone(counts),
  async flush() { await Promise.all([...pending]); deliver(summary()); await Promise.all([...pending]); summaryWritten = true; },
  reset() { counts = { emitted: 0, filtered: 0, errors: 0, sites: Object.create(null) }; summaryWritten = false; },
  httpSink(url, token) {
    return async event => {
      const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` }, body: JSON.stringify(event) });
      if (!response.ok) throw new Error(`LogForge collector returned ${response.status}`);
    };
  },
};
if (node) process.once("beforeExit", () => { if (!summaryWritten && (counts.emitted || counts.filtered)) deliver(summary()); });
