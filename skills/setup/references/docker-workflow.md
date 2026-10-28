# Docker-native setup and application containerization

Use this branch for Docker-native telemetry, application containerization, or a complete local LogForge stack. Work on the named project and its required services. Inspect existing files and running resources before choosing names, ports, mounts, or cleanup commands.

## Establish a working engine

Run `docker version`, `docker info`, and `docker compose version`. Distinguish missing software from a stopped daemon, permissions, unsupported container mode, or a remote context. The helper requires a local Linux-container engine. Preserve remote/production contexts; those need a separate deployment workflow.

When installation is authorized, carry out the appropriate current vendor workflow instead of only providing a link:

- Windows: [Docker Desktop installation](https://docs.docker.com/desktop/setup/install/windows-install/). Check architecture, Windows support, virtualization, and WSL requirements. Use the applicable per-user installation and start Desktop in the background. Handle required restart or interactive license prompts with the owner, then repeat engine checks.
- macOS: [Docker Desktop installation](https://docs.docker.com/desktop/setup/install/mac-install/). Select the matching Apple silicon/Intel package, use the vendor or configured package manager, start Desktop, and complete owner prompts.
- Linux: [Docker Engine installation](https://docs.docker.com/engine/install/). Read `/etc/os-release`, follow the official distribution-specific repository instructions, install Compose, and start the daemon. Docker-group membership grants substantial host privileges and must be intentional.

Inspect actual requirements before asking for any missing approval. Do not auto-accept agreements, purchase subscriptions, disable protections, or reboot without authorization. Do not assume Docker Desktop is free for every company. Execute authorized steps and identify precise remaining owner actions. Do not uninstall working Docker merely to simulate first installation.

Completion requires a working Linux engine and Compose, not just a CLI on PATH. Exercise a small appropriate container if the engine has not been tested. Record platform coverage honestly.

## Containerize the requested application

Inspect entry points, lockfiles, configuration, tests, databases/services, and deployment files. Adapt existing Dockerfiles/Compose first. Otherwise create project-specific definitions for the app and required services. Preserve supported runtime versions and lockfile-based builds. Add health checks/readiness, `.dockerignore` exclusions, named volumes for persistent data, and scoped names. Bind development ports to loopback and keep internal services on Compose DNS. Keep secrets out of images, build arguments, commits, reports, and command-line arguments.

Adapt listeners for container networking while retaining local defaults. Prefer non-root processes and limited writable paths where compatible. Containerize the actual application; do not substitute a toy app or include unrelated machine programs. Identify external-service boundaries and test only authorized integrations.

Validate Compose without printing expanded secrets, build images, start the scoped stack, and run the real application tests against it. Fix build, networking, readiness, and persistence errors. Preserve existing database volumes; avoid volume-removing cleanup. A Dockerfile alone is not completed containerization.

The bundled checkout reference includes `Dockerfile`, `compose.yaml`, `docker-checkout.py`, and `project.docker.logforge`. Its wrapper creates a unique Compose project, exercises the browser/backend/SQLite interaction, captures structured logs, and removes only its containers/network afterward. SQLite is intentionally in-memory in this demo; real databases need appropriate persistence. Browser automation runs on the host with a loopback collector.

## Provision and verify LogForge

Run `python <plugin-root>/scripts/logforge_setup.py start --monitor-docker` within authorization for Docker-host monitoring. It creates a separately owned appliance/network/volumes, trusted local CA configuration, protected read-only credentials, and an enrolled agent, and waits for online inventory. The agent accesses the Docker socket, container logs, and a read-only Docker host filesystem mount for host metrics; explain that scope before requesting any missing authorization. On Docker Desktop this is the Linux engine's host. The appliance itself has no Docker socket mount.

The helper uses pinned tested images, preserves existing connections/resources/reporting consent, and does not install Docker itself. The skill handles prerequisite installation and project adaptation. Fix reported prerequisites and rerun. Reconnect MCP to discover live tools.

Locate canonical inventory keys and enable only intended services: `python <plugin-root>/scripts/logforge_setup.py monitor --container-key <inventory-key>`. This uses the normal LogForge monitoring API and accepts only keys belonging to the managed agent. Add `--disable` to reverse it. Existing externally managed instances use their normal authorized API/UI flow. Use Docker's `json-file` driver where the file-tailing collector requires it. Preserve admission, scopes, and entitlements. Trigger a recognizable event and retrieve it through live MCP. Diagnose missing events from monitoring state, driver, connectivity, and ingestion instead of treating container health as proof.

Instrument a real feature with lazy `LogForge.out` calls across services, use the same feature tag, and execute its plan against those containers. Set `LOGFORGE_PROJECT_ROOT` so container-relative source paths map to the host project; use explicit browser URL mappings. Server stdout can flow to run capture and normal LogForge ingestion. Local report success alone is not Central retention evidence.

Deliver runnable project files, verified start/stop commands, the feature report, and observed live-log evidence. Distinguish verified behavior from untested OS installation paths or blocked external services.
