# Reviewer notes: publisher aggregates in 0.2.0

This release intentionally changes the previous opt-in default. Fresh Claude reporter state defaults to on, with conspicuous README/setup disclosure and a first-execution notice on stderr. No install hook or autonomous task is added. An explicit saved disable from 0.1.0 remains off; the release never resets another AI client's reporter state.

Run `python scripts/logforge_usage.py disable` before any diagnostic activity to save a persistent off choice without creating a reporting identity or making a network request. `status` and `preview` do not generate an identity. `LOGFORGE_USAGE_DISABLED=1` prevents recording and delivery in that executor environment. Only an explicit enable command reverses a saved disable.

The local bridge, relay and diagnostic runner summarize their own activity. The event/field allowlists in `scripts/logforge_usage.py` exclude source, snippets, prompts, chat history, logs, feature tags, project paths, tool arguments/results and credentials. The SDKs do not send publisher analytics. Counts use `logforge-claude-plugin` / `claude-directory` and a random reporting identity. They measure observed activity, not marketplace installs, unique people or verified downloads.

Local aggregate opt-out does not opt out of service processing needed for hosted queries. This package release does not deploy hosted operational request counters; that shared gateway rollout is separate. Account/security metadata and local diagnostic report contents have different retention from the aggregate usage receiver. See [usage privacy](usage-privacy.md) and [hosted privacy](hosted-privacy.md).

The release retains the existing read-only OAuth MCP tool contracts. Its telemetry tests cover fresh default-on, opt-out before first record, saved and legacy opt-out persistence, fresh-process persistence, environment opt-out, malformed/sensitive record rejection, payload minimization, destination/redirect boundaries and development delivery. Platform acceptance is external and is not inferred from passing tests.
