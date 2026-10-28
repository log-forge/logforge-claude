# Hosted account and private relay

The public plugin uses https://usage.logforge.dev/mcp with OAuth. Use the client's normal Connect flow to sign into LogForge. Credentials belong in that flow, never chat. The gateway account is separate from a self-hosted Central administrator account.

From a desktop executor, finish any requested local Docker or remote-agent setup first. Then run:

```text
python <plugin-root>/scripts/logforge_cloud.py connect --name "Development machine" --no-browser
```

Open the returned verification URL for the user. They sign in or create an account and approve the named connection. The command waits up to ten minutes. If approval expires, rerun it. The local Central key stays on the machine. Pairing grants the signed-in account read-only access to the selected Central connection; it never grants shell access, deployment authority, or a way to select arbitrary local files.

Explain that requested logs, metrics, inventory, and explicitly shared reports pass through the hosted gateway to the requesting client. The gateway holds responses temporarily in memory, not a diagnostic archive. This service processing is separate from optional publisher usage reporting. Refer to [hosted privacy](../../../docs/hosted-privacy.md).

Share a project only when the user authorizes remotely accessing its reports, including their application data and source excerpts:

```text
python <plugin-root>/scripts/logforge_cloud.py share-project <absolute-project-path> --name "Checkout"
python <plugin-root>/scripts/logforge_cloud.py start
python <plugin-root>/scripts/logforge_cloud.py status
```

The returned project UUID is used with hosted `logforge_runs_list` and `logforge_run_read`. Call `logforge_connections_list` first and select the intended instance. Never send absolute local project paths as hosted tool arguments. Reports remain local unless requested through a shared project.

`start` launches a background relay that survives closing the chat; it does not install an operating-system startup service. `serve` runs in the foreground. For unattended operation, configure the platform's user service or scheduled task within the authorized scope, using the absolute Python and script paths and the same protected configuration. Disclose when reboot persistence has not been configured. Restart the relay after replacing the local Central connection.

```text
python <plugin-root>/scripts/logforge_cloud.py stop
python <plugin-root>/scripts/logforge_cloud.py unshare-project <project-id>
```

Revoke a connection at https://usage.logforge.dev/gateway to immediately reject its credential. Credentials expire after 30 days; reconnect after expiration. Stop the relay and preserve or remove only its own cloud configuration before pairing a replacement. Do not delete the Central credentials or project reports. `LOGFORGE_CLOUD_CONFIG` can select an isolated protected configuration for tests.

The relay makes outbound HTTPS requests on port 443. It needs no inbound port on the user's workstation. Remote collectors still need their normal route to Central; follow [remote-agent deployment](remote-agents.md) for those separate network requirements. A hosted MCP server does not make an unreachable Central or remote device reachable automatically.

When no local executor or SSH access is available, provide these device-specific steps for the owner and verify the linked connection after they finish. Do not claim to have installed Docker, edited the project, or deployed a collector from a web-only client.
