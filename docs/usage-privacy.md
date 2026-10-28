# Publisher usage reporting — on by default

LogForge is a product of **17811365 Canada Incorporated**. From Claude plugin 0.2.0, minimal aggregate publisher reporting is ON by default for a fresh reporter profile. It starts after local tool or diagnostic activity, not installation. Existing explicit saved disable choices remain off across updates. A one-time local execution notice identifies the default and off switch. It is separate from your application telemetry and self-hosted LogForge connection. Disabling it does not limit plugin functionality.

While enabled, the plugin sends daily cumulative usage totals to `https://usage.logforge.dev/v1/plugin-reports`: a random reporting identity, the UTC day of first observed use, reporting day, plugin version, operating-system family, selected development/production environment and distribution label, bridge starts, connection successes/failures, allowlisted tool call counts, failures, total durations and latency buckets, diagnostic pass/fail counts, output/check/filter/unreached counts, and the languages exercised (JavaScript, TypeScript, Python). Unknown tool names become `other`. Distribution is self-reported; it is not proof of an Anthropic install.

It does not send source code, source snippets, filenames, project names, repository URLs, log contents, `.logforge` documents, feature tags, prompts, tool arguments/results, queries, raw errors, credentials, hostnames, emails, or Anthropic account identifiers. The application SDKs do not send publisher analytics themselves. The local bridge and diagnostic runner summarize their own activity.

The random reporting token authenticates subsequent reports for the same identity. It is not your LogForge MCP key. On Windows the token uses user-bound DPAPI; elsewhere the state directory/file is owner-only. Local usage state is outside the installed plugin and survives version updates. At most 100 daily/version report snapshots are retained locally, with at most nine UTC days of history; transient failures are retried during later usage. This is best-effort reporting, not an audit log. Abrupt termination or persistent local storage failure can lose counts.

The receiver keeps the latest cumulative report per identity/day/version/distribution/environment until the operator deletes it; there is no automatic server retention deadline. It stores only a hash of the reporting token. Reports are available through the private, authenticated usage dashboard. The HTTP service necessarily processes connection metadata, including the network source address, while receiving requests; the report payload does not contain it. LogForge's receiver disables access logging and uses short-lived hashed addresses for rate limiting. Hosting/network infrastructure may have separate operational records.

Run these commands from the installed plugin directory, using Python 3.10 or newer:

```text
python scripts/logforge_usage.py status
python scripts/logforge_usage.py enable
python scripts/logforge_usage.py preview
python scripts/logforge_usage.py disable
```

`status` and `preview` show the current setting without creating a reporting identity. `preview` shows pending report payloads without the reporting token. `disable` saves an explicit persistent off choice, including before first activity, stops future reports and discards the pending local queue. It creates no reporting identity when used before first activity. `LOGFORGE_USAGE_DISABLED=1` also suppresses local recording and delivery before execution; the CLI disable choice persists without that environment variable. A request already in flight may finish. It does not erase reports already received. Re-enabling after disabling starts a new random reporting identity; changing the destination also resets the identity and discards pending reports so credentials/data never cross destinations. A copied state file can share an identity, so reporting identities are not a count of distinct people or machines.

For questions or deletion of received usage reports, contact [adan@logforge.dev](mailto:adan@logforge.dev) and provide the random `instance_id` shown by `status`, never your reporting token or MCP key. Save that identifier before resetting or removing local state. The operator must verify the request before deleting records.

For a development receiver, explicitly select it:

```text
python scripts/logforge_usage.py enable --environment development --endpoint http://127.0.0.1:58001/v1/plugin-reports
```

HTTPS with normal certificate validation is required except on loopback. Redirects are refused. The default distribution follows `distribution.json`: `local` for the local development override and `claude-directory` for the directory release build. `--distribution` explicitly overrides it. This identifies the distributed build, not the installation mechanism: manually copying a directory build retains its label. It is not a verified installation receipt.

The `.logforge` reports generated for your development task can contain code and application data. They stay in the project unless you share them or Claude reads them to perform your request. Tool results read by Claude are subject to your Claude/Anthropic workspace settings and policies. The separate publisher usage feed never uploads those reports.

This Claude build uses a separate local reporting state and the product label `logforge-claude-plugin`. It does not import preferences from another AI client. Only an explicit enable command re-enables a saved disable choice. The legacy `enable --consent` flag remains accepted for compatibility, but is no longer required. Hosted connector calls do not invoke this local publisher-reporting helper. Hosted account/security processing follows the service privacy notice; this local feed does not measure hosted requests or marketplace installs.
