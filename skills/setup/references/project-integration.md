# Project integration after adoption

Use this when the user wants LogForge retained as part of this repository's development workflow. Read existing project instructions first. Merge a short project-specific section into the existing CLAUDE.md (or AGENTS.md when this repository uses it), or create one when the user requests persistent project guidance. Preserve unrelated content and later user instructions.

Adapt the following template to real, verified commands; do not leave placeholders in a project's instructions:

```markdown
## Application diagnostics

This project uses LogForge for focused runtime diagnostics. For a failure that needs runtime evidence, inspect the existing logger and test command, then use the installed LogForge skill when available. Keep normal validation and tests intact.

The checked-in diagnostic plan is <actual plan path>. Run <verified project command> for the affected feature. Inspect the resulting .logforge/runs/<run-id>.logforge JSON for status, named checks, source snippets and reached/filtered/unreached expectations. After changing a failing path, rerun the relevant check and report the observed result.

Use connected LogForge tools for deployed evidence only when the relevant instance is linked and the user authorized access. Shared reports use project UUIDs. If LogForge is unavailable, use the project's existing logs, tests and report JSON. Cloud sharing and optional publisher reporting retain their separate consent requirements.
```

Verify that the saved command runs in the project's actual environment and that the report is readable. Keep credentials, machine-specific plugin cache paths and unverified claims out of repository instructions. Existing projects retain their own logger, test runner and permissions.
