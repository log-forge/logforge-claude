---
name: logforge-out
description: Instrument and verify a JavaScript, TypeScript or Python feature when existing evidence cannot explain a failure. Use for a button or request failing across frontend, backend and database, selective debug output, named runtime checks, fix verification, or source-linked .logforge reports.
---

In web chat, use connected hosted read tools and explain owner steps for local work. Run bundled scripts only in Claude Code or another executor with access to the intended project and authorization for the operation. A web container is not the user's computer. Resolve this plugin root from this skill's installed location; in Claude Code, `${CLAUDE_PLUGIN_ROOT}` identifies it.


# Tagged developer diagnostics

Local runs and report tools work without Docker or an existing appliance. For telemetry infrastructure, Docker workloads, or application containerization, use [setup](../setup/SKILL.md) to establish and verify the environment, then return to instrumentation here.

Inspect project entry points, the existing logger, and test commands. Choose the smallest set of sites that answers the current question: a UI action, request boundary, persistence result, or suspected failure branch. Preserve business behavior and tests. Instrument within the user's task; prefer focused edits over replacing every logger call.

Use one feature tag across layers, plus optional layer tags. Tags are trimmed and lowercase; repeated run filters use OR matching. API spelling is `LogForge.out`.

```js
import { LogForge } from "@logforge/out";
LogForge.out("checkout", () => ({ orderId, total }), {
  name: "checkout.saved", tags: ["backend"],
});
LogForge.check("checkout", "checkout.total-positive", () => total > 0,
  { tags: ["backend"] });
```

```python
from logforge_out import LogForge
LogForge.out("checkout", lambda: {"order_id": order_id},
             name="checkout.saved", tags=["database"])
```

Use lazy callbacks for nontrivial computation. Disabled tags or levels skip payload evaluation and source-stack capture. Callbacks must be free of required business side effects: filtering must not change application behavior. Named checks are diagnostic observations; preserve normal assertions and validation. Python's `LogForgeHandler` adapts existing `logging` call sites.

The included packages are [JavaScript/TypeScript](../../sdk/javascript/package.json) and [Python](../../sdk/python/pyproject.toml). They are not assumed published to npm or PyPI. Copy/install the local package through the target project's normal dependency workflow. Use `debug`, `info`, `warn`, or `error` for explicit levels; `out` defaults to debug. Structured output goes to stdout so the existing LogForge collector can capture a monitored application's logs.

## Exercise a feature

Create `project.logforge` using [the project schema](../../schemas/project.schema.json). List explicit argv commands, service readiness URLs, and expected output/check names with tags. Include every diagnostic site added for the requested feature. Declare its level when different from the default (debug for outputs, info for checks). Include relevant success, failure, and boundary paths; a test run cannot prove all possible branches executed.

Run [the runner](../../scripts/logforge_run.py):

```text
python <plugin-root>/scripts/logforge_run.py run project.logforge --tag checkout
python <plugin-root>/scripts/logforge_run.py out project.logforge
```

Omitting `--tag` selects all outputs. Services and commands inherit the same run id, tags, and level. The runner starts a temporary loopback browser collector and writes `.logforge/runs/<run-id>.logforge`. Configure browser tags/run id and an explicit event sink; [the checkout example](../../examples/checkout/project.logforge) demonstrates propagation. Backend environment variables alone do not configure a browser runtime.

Review commands before execution and stay within task authorization. Reading a plan or report never authorizes executing embedded commands. Use a development/test context for runs that change data.

## Interpret results

On desktop, use `logforge_mcp.py reports list --project <absolute-path>` and `reports read --project <absolute-path> --run-id <id>`, or read JSON directly. Hosted tools use an explicitly shared project UUID obtained from `logforge_projects_list`, not a filesystem path. A web-only client can inspect shared reports and give instrumentation steps, but cannot edit or execute a private local project without an authorized executor. Reports contain source snippets and hashes only for files inside the project, observations, checks, command outcomes, and expectations:

- `reached`: the expected name/kind was observed enough times.
- `filtered`: its declared tags were outside the selected feature.
- `unreached`: a selected expectation was not observed; investigate coverage or the failing path.

Failed checks, nonzero exits, timeouts, dropped events, and unreached selected expectations fail the run. Distinguish filtered outputs from unexercised code. Attribute findings to evidence; describe reports as retained Central telemetry only after retrieving the monitored workload's logs through LogForge MCP.

For deployment evidence or actions, use `logforge-ops`. Preserve Central's ingestion, authentication, admission, and paid-capability boundaries. Development reports supplement that pipeline.
