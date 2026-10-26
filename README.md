# DocDrift

**Does this repo do what its README says?**

DocDrift is a command-line agent, not a dashboard. It inspects a public GitHub repo, turns runnable README instructions into a reviewable plan, and can execute approved checks in a disposable Docker sandbox while recording output as evidence.

> This is an early alpha. It deliberately starts with concrete, testable README instructions rather than claiming to understand every sentence.

## Quick start

Requirements: Python 3.10+, `git`. Docker Engine/Desktop is required only to execute commands.

Run directly from a checkout:

```bash
python docdrift.py --version
python docdrift.py https://github.com/owner/repo
```

Or install an isolated command with `pipx install .` and use `docdrift https://github.com/owner/repo`.

The default is **plan only**. It shallow-clones the repo, scans its root and nested README files, inventories common project files and test paths, and writes `docdrift-report.md` with the plan.

To execute, review the plan and opt in:

```bash
python docdrift.py https://github.com/owner/repo --execute
```

You must type `RUN` interactively. Commands are sequenced in one sandbox so dependency installation can affect later tests. The container has a 2 GB memory cap, 2 CPU cap, process limit, dropped Linux capabilities, no added privileges, an unprivileged user, and a writable mount only for the disposable clone. Network access is disabled by default.

Dependency installation often needs network access; only enable it for a repository you trust:

```bash
python docdrift.py https://github.com/owner/repo --execute --allow-network
```

Machine-readable report and a longer timeout:

```bash
python docdrift.py https://github.com/owner/repo --execute --allow-network --repair \
  --timeout 240 --report reports/audit.md --json reports/audit.json
```

## Current capabilities

- Shallow clone and pinned short commit hash for the audit evidence.
- Scans the root README plus nested README files while skipping common vendored/build directories; extracts allowlisted commands from fenced shell blocks and simple inline code.
- Simple repository inventory: common language manifests, likely test paths, and top-level files.
- Categorizes setup, tests, quickstart, and local endpoint commands; recognizes localhost URLs and documented API paths.
- Runs finite documented commands in one shared sandbox, then launches recognized server commands detached and probes documented localhost URLs. Local endpoint probes use the container's Python or Node runtime.
- Records exit code, duration, stdout/stderr, README file/line, and status (`VERIFIED`, `FALSIFIED`, `UNVERIFIABLE`, or `PLANNED`) in Markdown and optional JSON.
- Skips obvious hazardous commands and flags recursive DocDrift examples, placeholder URLs, likely GUI, and incomplete commands as unverifiable instead of trying to run them.
- With `--repair`, attempts one narrow fix: if a documented Python entrypoint is missing but exactly one matching basename exists elsewhere in the repo, reruns the corrected path in the sandbox. The original verdict remains falsified if the README's original command was wrong.

## Limits (honest ones)

- Only commands and endpoint-shaped claims are extracted. Prose-only promises, workflows, and implicit setup requirements are not yet converted into verifiable claims.
- The supported command set is intentionally narrow. A skipped command is not proof that a claim is false.
- Server launch detection is heuristic. Endpoints are only probed when a recognized README server command and localhost URL/path are present. `--base-url` can override the host/port while preserving the documented path.
- A zero exit code verifies that exact command in this environment. It does not prove semantic correctness or that a test suite is comprehensive.
- Repair is deliberately narrow and execution-only: it retries a uniquely located Python script path; it does not edit the README or invent arbitrary code changes. General repair patches, broader self-healing, remote GitHub integration, and LLM-based claim interpretation are not implemented yet.

## Safety

README commands are untrusted code. Docker provides a useful containment layer, not a perfect security boundary. Keep Docker patched; do not mount your home directory, credentials, or Docker socket. Network is off by default; `--allow-network` gives sandboxed code outbound access. Do not execute repos you do not trust.

## Checks for this tool

```bash
python -m unittest discover -s tests -v
```
