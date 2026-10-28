# Changelog

## 0.2.0

Publisher aggregate reporting is on by default for fresh Claude reporter state. Existing explicit disables remain off. The persistent `logforge_usage.py disable` command works before first activity without generating a reporting identity; `LOGFORGE_USAGE_DISABLED=1` suppresses recording and delivery through the executor's environment.

The README, setup guidance, first-execution notice and privacy documentation describe the default, allowlisted fields, receiver, retention and off switch. No source, snippets, prompts, logs, feature tags, arguments/results or credentials enter aggregate reports. Local aggregates, hosted operational metrics and marketplace installation statistics remain distinct. This material data-practice update requires renewed platform review.

## 0.1.0

Initial Claude adapter with hosted OAuth MCP, setup/operations/diagnostic skills, local JavaScript/TypeScript/Python tooling, source-linked reports and opt-in aggregate reporting.
