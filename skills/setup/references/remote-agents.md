# Deploy remote LogForge agents

Use this workflow when the user asks to add monitoring to named remote machines or a selected fleet. Keep the existing local setup path for local Docker. An SSH host is a deployment target, while Central is the telemetry destination; distinguish them explicitly.

## Establish access and reachability

Resolve the intended hosts, SSH identities, monitored applications, and Central instance from authorized project/deployment configuration. Use existing SSH aliases/agents or local key paths. Keep private keys out of chat and the deployment plan. Check host fingerprints through a trusted channel and preserve `StrictHostKeyChecking=yes`. A connection failure is not permission to disable host verification.

Inspect each target's OS, Python 3, Linux Docker engine, available disk/memory, and Docker permissions. Install/start missing prerequisites using current official distribution instructions within the user's authorization. The bundled deployment helper detects missing prerequisites; the skill owns installation and recovery. Work through required administrator actions with the owner. For other operating systems, Kubernetes, or restricted managed platforms, use that platform's normal agent deployment mechanism and verify it separately; the helper covers Linux Docker over SSH.

Agents need a stable, reachable Central HTTPS bootstrap endpoint and mTLS agent endpoint. The managed local appliance binds to loopback by default and is not directly reachable from another machine. Prefer an existing Central deployment or approved private network connection. If networking must change, first identify the exact listener, routing, DNS, certificate names, and firewall scope. A user-requested remote rollout authorizes the named targets; it does not justify publishing all local services to the internet. Keep certificates verified and credentials private.

## Remote devices and network changes

Build a small connection map before changing ports. Resolve the actual listening ports and advertised addresses from Central's configuration; container ports and router/public ports can differ.

| Initiator | Destination | Purpose | Common port, to verify |
| --- | --- | --- | --- |
| Claude deployment machine or approved jump host | Selected remote machine | SSH setup and maintenance | TCP 22 |
| Remote agent | Central HTTPS endpoint | Trusted bootstrap/enrollment | TCP 443 |
| Remote agent | Central agent endpoint | mTLS control, inventory, and telemetry | TCP 8443 |

The agent normally initiates its Central connections, so opening an inbound telemetry port on every device is unnecessary. Verify outbound DNS and endpoint access from both the target host and its agent container. Keep the Docker daemon socket local. Account for host firewalls, cloud security groups, router NAT, private-network routes, and certificate hostnames separately; a successful localhost test does not prove remote reachability.

If a rule is missing and the requested setup authorizes the change, configure the specific rule through available provider tools or the host's firewall. Restrict allowed source machines/networks, preserve SSH access, and record which rule was created and how to reverse it. If the proposed solution adds a public listener or a new paid network service, explain that concrete change and obtain missing authorization before applying it. Prefer private connectivity when it fits the user's deployment. When NAT or carrier-grade NAT prevents inbound access, explain the private-network or hosted-Central alternative instead of repeatedly opening ports.

For a device the user must operate, provide a short handoff tailored to its OS or router/cloud product: the exact package/image to install, the exact destination hostname/port, the source restriction, the device-specific command or verified UI steps, the expected success signal, and the rollback step. Resolve current vendor instructions before prescribing installation or UI actions. Keep credentials in the protected local prompt/configuration; the owner should not paste keys or enrollment secrets into chat. State what remains waiting on the owner, continue independent work, then resume verification when they finish.

Check device architecture and runtime support before proposing the Docker agent. For an unsupported NAS/router/Windows-native/embedded target, state the limitation and select a supported nearby collector or supported forwarding integration after inspecting that device's documented capabilities. Do not claim the Linux Docker helper installs a native agent on every device. Finish with an online agent, inventory, and a retained test event from the intended source, and record outstanding networking or owner steps alongside the `.logforge` deployment report.

## Prepare a concrete deployment plan

Create a project-local `remote-agents.logforge` following [the schema](../../../schemas/remote-agents.schema.json). Generate a fresh UUID for `deployment_id` once and preserve it for retries. Use a distinct `agent_name` for every host. Include only explicit targets, public endpoint addresses, optional SSH credential/known-hosts file paths, and optional private DNS `extra_hosts` mappings. Include no passwords, private keys, or enrollment tokens.

```json
{
  "schema_version": "1.0",
  "kind": "remote-agents",
  "deployment_id": "REPLACE_WITH_A_NEW_UUID",
  "name": "Checkout servers",
  "central": {
    "url": "https://central.example.com/unicron",
    "mtls_url": "https://central.example.com:8443",
    "admin_config": "/private/config/logforge/remote-admin.json",
    "ca_file": "/private/config/logforge/root_ca.crt"
  },
  "targets": [
    {"agent_name": "checkout-api-01", "host": "checkout-api-01", "username": "ubuntu"}
  ]
}
```

Confirm endpoint ports from the actual Central deployment. The example ports are illustrative. Local SSH aliases can carry jump-host configuration; the helper preserves them. Paths are resolved relative to the plan when not absolute. Use `extra_hosts` only for an intentional private-DNS mapping needed inside the agent container; the SSH host must also resolve Central for the preflight.

For a helper-managed Central profile, omit `admin_config` and `ca_file`; existing protected administrator state and the owned CA are reused. Otherwise configure administrator access with `logforge_remote.py configure-central --url <Central HTTPS URL ending /unicron> --username <administrator> --ca-file <trusted CA> --output <private config path>`. The password is entered through a hidden local prompt and protected with Windows DPAPI or owner-only filesystem permissions. The read-only MCP key stays read-only. Administrator enrollment still goes through LogForge's normal permission, entitlement, and agent-limit checks.

## Deploy, verify, and recover

Run `python <plugin-root>/scripts/logforge_remote.py preview <plan>`. Inspect per-host preflight results before applying. Reuse the user's existing authorization when the plan matches their request; obtain any missing permission for newly introduced hosts, installation, or network exposure. Agent monitoring requires the target's Docker socket, container logs, and a read-only Docker-host root mount for metrics.

Run `logforge_remote.py apply <plan>`. For each host, the helper verifies SSH and Central reachability, enrolls a new agent if necessary, transfers a temporary token through encrypted SSH stdin, launches the pinned agent image, and waits for Central to report online inventory. SSH private keys remain on the controller. Repeated runs reuse matching owned containers and certificates; they preserve volumes and refuse conflicting/unowned resources or a duplicate Central identity. A partial failure yields a failed report while retaining each host's individual result. Fix the failing hosts and rerun the same plan.

The helper uses the standard administrator enrollment API. Current MCP `logforge_agent_deploy_preview`/`execute` tools are limited to the initial-agent scenario on the inspected Central version. Do not remove an existing agent just to satisfy that restriction. When using advertised MCP deployment tools instead, preserve their scope, Action Mode, preview/execute contract, and any required confirmation.

Successful deployment proves online inventory, not application log retention. Find intended workloads using `logforge_inventory_search` and their canonical container keys. Enable monitoring through the normal authorized Central workflow, trigger a recognizable event, and retrieve it with `logforge_logs_query`. Then use `logforge-out` to instrument/exercise the requested cross-service feature. Report which hosts and services passed, failed, or remained untested.

Every helper operation writes `.logforge/runs/<run-id>.logforge`, readable through the existing MCP report tools, with per-host checks and explicit coverage limits. Enrollment secrets and SSH key contents are excluded. `logforge_remote.py stop <plan>` stops only matching owned agents and retains their volumes; `apply` resumes them. Changed specifications require an explicit migration; the helper does not silently replace containers or destroy certificates/data.
