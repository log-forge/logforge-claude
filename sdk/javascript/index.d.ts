export type Level = "debug" | "info" | "warn" | "error";
export interface Source { file: string; line: number; column?: number; language?: string; }
export interface Options { tags?: string[]; level?: Level; name?: string; id?: string; source?: Source; }
export interface Event { _logforge: "event"; schema_version: "1.0"; stream: "logforge.out"; event_id: string; run_id: string | null; timestamp: string; service: string; kind: "output" | "check"; name: string; site_id: string | null; tags: string[]; level: Level; source: Source | null; data: unknown; passed?: boolean; }
export interface Stats { emitted: number; filtered: number; errors: number; sites: Record<string, { name: string; tags: string[]; emitted: number; filtered: number }>; }
export interface Summary { _logforge: "summary"; schema_version: "1.0"; run_id: string | null; service: string; timestamp: string; stats: Stats; }
export interface Configuration { tags?: string[]; level?: Level; enabled?: boolean; runId?: string; service?: string; sink?: (event: Event | Summary) => void | Promise<void>; }
type Payload = unknown | (() => unknown);
export declare const LogForge: {
  configure(options: Configuration): typeof LogForge;
  out(tag: string, payload: Payload, options?: Options): Event | undefined;
  out(payload: Payload, options?: Options): Event | undefined;
  debug(tag: string, payload: Payload, options?: Options): Event | undefined;
  info(tag: string, payload: Payload, options?: Options): Event | undefined;
  warn(tag: string, payload: Payload, options?: Options): Event | undefined;
  error(tag: string, payload: Payload, options?: Options): Event | undefined;
  check(tag: string, name: string, predicate: boolean | (() => boolean), options?: Options): Event | undefined;
  isEnabled(tag: string | string[], level?: Level): boolean;
  stats(): Stats;
  flush(): Promise<void>;
  reset(): void;
  httpSink(url: string, token: string): (event: Event | Summary) => Promise<void>;
};
