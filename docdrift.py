#!/usr/bin/env python3
"""DocDrift: audit what a repo's README asks a user to do.

Plan-only by default. Repository commands execute only with --execute and a typed RUN.
Python 3.10+, git; Docker is required to execute.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

VERSION = "0.3.0"
SUPPORTED = {"python", "python3", "pip", "pip3", "pytest", "uv", "poetry", "npm", "npx", "pnpm", "yarn", "node", "make", "cargo", "go", "bundle", "ruby", "php", "composer", "curl"}
LANGS = {"", "sh", "shell", "bash", "zsh", "console", "terminal", "cmd", "powershell", "shellsession"}
DANGEROUS = re.compile(r"(?:^|[;&|]\s*)(?:sudo|su|shutdown|reboot|mkfs\b|dd\s+if=|docker\s+run|podman\s+run)\b|:\(\)\s*\{\s*:\s*\|\s*:\s*;?\s*\}|rm\s+-rf\s+(?:/|~)|(?:curl|wget)\b[^\n|]*\|\s*(?:sh|bash)", re.I)
URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?(?:/[\w./?&=%#:+~-]*)?", re.I)

@dataclasses.dataclass
class Step:
    id: str
    command: str
    section: str
    line: int
    category: str
    risky: bool = False
    reason: str = ""
    status: str = "PLANNED"
    source_file: str = "README.md"
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    repair_command: str | None = None
    repair_status: str | None = None
    repair_stdout: str = ""
    repair_stderr: str = ""


def normalize_github(value: str) -> str:
    value = value.strip().rstrip("/")
    m = re.fullmatch(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:\?.*)?", value)
    if not m:
        raise argparse.ArgumentTypeError("Expected a public GitHub URL like https://github.com/owner/repo")
    return f"https://github.com/{m.group(1)}/{m.group(2)}.git"


def safe_text(s: str, limit: int = 12000) -> str:
    s = s.replace("\x00", "")
    return s if len(s) <= limit else s[:limit] + f"\n… truncated ({len(s) - limit} chars omitted)"


def classify(command: str, heading: str) -> str:
    s = f"{command} {heading}".lower()
    if command_head(command) == "curl":
        return "endpoint"
    if re.search(r"pytest|unittest|\btests?\b|vitest|jest|cargo test|go test", s):
        return "tests"
    if re.search(r"\b(install|requirements|dependencies|setup)\b|pip install|npm ci|npm install|pnpm install|yarn install|poetry install|uv sync", s):
        return "install"
    if re.search(r"\b(run|start|launch|quickstart|getting started|usage|serve)\b", s):
        return "quickstart"
    return "command"


def command_head(command: str) -> str:
    # Ignore leading shell assignments and common documented prompt markers.
    s = re.sub(r"^(?:\$|>|>>>|PS>)\s*", "", command.strip())
    s = re.sub(r"^(?:[A-Za-z_][\w]*=[^ ]*\s+)+", "", s)
    parts = re.split(r"\s+|&&|\|\||[|;]", s, maxsplit=1)
    head = parts[0] if parts else ""
    if head in {"cd", "export", "set", "source", "."}:
        m = re.search(r"(?:&&|;|\|\|)\s*([\w./-]+)", s)
        if m:
            head = m.group(1)
    return head


def is_supported(command: str) -> bool:
    head = command_head(command)
    if head == "curl":
        # Only locally documented probes are safe/meaningful in the isolated runner.
        return bool(re.search(r"(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?(?:/|\b)", command, re.I))
    if head in SUPPORTED:
        return True
    # Accept command chains beginning with a directory change.
    return bool(re.match(r"^(?:cd\s+[^;&|]+\s*&&\s*)?(?:" + "|".join(map(re.escape, SUPPORTED - {"curl"})) + r")\b", command.strip()))


def extract_steps(readme: Path, source_file: str | None = None) -> list[Step]:
    lines = readme.read_text(encoding="utf-8", errors="replace").splitlines()
    steps: list[Step] = []
    section = "README"
    fence_char: str | None = None
    fence_len = 0
    lang = ""
    body: list[tuple[int, str]] = []
    for number, line in enumerate(lines, 1):
        hm = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if hm and fence_char is None:
            section = hm.group(1).strip()
        fm = re.match(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)[^\n]*$", line)
        if fm:
            marker = fm.group(1)
            if fence_char is None:
                fence_char, fence_len, lang = marker[0], len(marker), fm.group(2).lower()
                body = []
            elif marker[0] == fence_char and len(marker) >= fence_len:
                if lang in LANGS:
                    # Join backslash continuations; preserve README line provenance.
                    grouped: list[tuple[int, str]] = []
                    pending = ""
                    firstline = 0
                    for ln, raw in body:
                        t = raw.rstrip()
                        if not pending:
                            firstline = ln
                        pending += t[:-1] + " " if t.endswith("\\") else t
                        if t.endswith("\\"):
                            continue
                        grouped.append((firstline, pending.strip()))
                        pending = ""
                    if pending:
                        grouped.append((firstline, pending.strip()))
                    for ln, raw in grouped:
                        cmd = re.sub(r"^(?:\$|>|>>>|PS>)\s*", "", raw.strip())
                        if not cmd or cmd.startswith(("#", "//")) or not is_supported(cmd):
                            continue
                        # Keep server commands in the plan, but never block on them as foreground commands.
                        cat = classify(cmd, section)
                        risky = bool(DANGEROUS.search(cmd))
                        step = Step(f"C{len(steps)+1:03}", cmd, section, ln, cat,
                                    risky, "command trips safety pattern" if risky else "",
                                    status="UNVERIFIABLE" if risky else "PLANNED",
                                    source_file=source_file or readme.name)
                        if not any(s.command == step.command for s in steps):
                            steps.append(step)
                fence_char, lang, body = None, "", []
            continue
        if fence_char:
            body.append((number, line))
    # README prose often puts a single command inline rather than in a fence.
    inline_section = "README"
    in_any_fence = False
    for number, line in enumerate(lines, 1):
        hm = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if hm and not in_any_fence:
            inline_section = hm.group(1).strip()
        if re.match(r"^\s*(`{3,}|~{3,})", line):
            in_any_fence = not in_any_fence
            continue
        if in_any_fence:
            continue
        for match in re.finditer(r"`([^`\n]+)`", line):
            cmd = match.group(1).strip()
            if is_supported(cmd) and not any(s.command == cmd for s in steps):
                risky = bool(DANGEROUS.search(cmd))
                steps.append(Step(f"C{len(steps)+1:03}", cmd, inline_section, number,
                                  classify(cmd, inline_section), risky,
                                  "command trips safety pattern" if risky else "",
                                  status="UNVERIFIABLE" if risky else "PLANNED",
                                  source_file=source_file or readme.name))
    return steps


IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "vendor", "dist", "build", "coverage", "target", "__pycache__"}


def repo_files(repo: Path, max_files: int = 100000):
    seen = 0
    for base, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d.lower() not in IGNORED_DIRS)
        for name in files:
            path = Path(base) / name
            if path.is_symlink():
                continue
            yield path
            seen += 1
            if seen >= max_files:
                return


def find_readmes(repo: Path) -> list[Path]:
    names = {"readme.md", "readme.mdx", "readme.rst", "readme.txt", "readme"}
    root = [repo / n for n in ("README.md", "README.MD", "readme.md", "README.rst", "README", "README.txt") if (repo / n).is_file()]
    nested = [p for p in repo_files(repo, 100000) if p.parent != repo and p.name.lower() in names]
    ordered = root[:1] + sorted(nested, key=lambda p: str(p).lower())
    return ordered[:25]



def inventory(repo: Path) -> dict[str, Any]:
    roots = [p.name for p in repo.iterdir() if p.name not in {".git", "node_modules", ".venv", "venv", "dist", "build"}]
    markers = {
        "Python": ["pyproject.toml", "requirements.txt", "Pipfile", "setup.py", "setup.cfg"],
        "Node.js": ["package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json"],
        "Rust": ["Cargo.toml"], "Go": ["go.mod"], "Ruby": ["Gemfile"], "PHP": ["composer.json"],
        "Docker": ["Dockerfile", "compose.yaml", "docker-compose.yml"],
        "CI": [".github/workflows"],
    }
    stacks = [name for name, paths in markers.items() if any((repo / p).exists() for p in paths)]
    if list(repo.glob("*.py")) and "Python" not in stacks:
        stacks.insert(0, "Python")
    tests = []
    for p in repo_files(repo, 100000):
        if (re.search(r"(^|/)(tests?|specs?)(/|$)", str(p.relative_to(repo)), re.I) or re.search(r"(test|spec)\.[^.]+$", p.name, re.I)):
            tests.append(str(p.relative_to(repo)))
            if len(tests) >= 12:
                break
    return {"top_level": sorted(roots)[:40], "stacks": stacks, "test_files": tests}


def endpoint_claims(readme: Path, source_file: str | None = None) -> list[dict[str, Any]]:
    text = readme.read_text(encoding="utf-8", errors="replace")

    def expected_body(line: str) -> str | None:
        match = re.search(r"\{[^{}]{1,500}\}", line)
        return match.group(0) if match else None
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n, line in enumerate(text.splitlines(), 1):
        # Documented localhost URLs; skip unrelated external hosts.
        for url in URL_RE.findall(line):
            if url not in seen:
                seen.add(url)
                claims.append({"id": f"E{len(claims)+1:03}", "url": url.rstrip(".,);`"), "line": n, "file": source_file or readme.name, "source": line.strip(), "expected": expected_body(line), "status": "UNVERIFIABLE", "evidence": "Endpoint probing requires a documented server start command."})
    # Relative route declarations: probe only when a localhost base is documented or explicitly supplied later.
    bases = [c["url"] for c in claims]
    paths = re.compile(r"(?:(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+)?(?<![\w/])/(?:api(?:/[\w{}:-]+)*|healthz?|v\d+/[\w{}:-]+(?:/[\w{}:-]+)*)", re.I)
    for n, line in enumerate(text.splitlines(), 1):
        for path in paths.findall(line):
            if not any(path in c["url"] for c in claims):
                url = bases[0].rstrip("/") + path if bases else path
                claims.append({"id": f"E{len(claims)+1:03}", "url": url, "line": n, "file": source_file or readme.name, "source": line.strip(), "expected": expected_body(line), "status": "UNVERIFIABLE", "evidence": "Relative endpoint found; set --base-url to probe it." if not bases else "Relative endpoint found; response not yet checked."})
    return claims[:30]


def select_image(repo: Path, override: str | None) -> str:
    if override:
        return override
    if (repo / "package.json").exists(): return "node:22-bookworm-slim"
    if (repo / "pyproject.toml").exists() or (repo / "requirements.txt").exists() or list(repo.glob("*.py")): return "python:3.12-slim"
    if (repo / "Cargo.toml").exists(): return "rust:1-slim"
    if (repo / "go.mod").exists(): return "golang:1.24-bookworm"
    if (repo / "Gemfile").exists(): return "ruby:3.4-slim"
    if (repo / "composer.json").exists(): return "php:8.4-cli"
    return "ubuntu:24.04"


def run_container_step(name: str, step: Step, timeout: int) -> None:
    started = time.monotonic()
    try:
        p = subprocess.run(["docker", "exec", name, "sh", "-lc", step.command], text=True, capture_output=True, timeout=timeout)
        step.exit_code = p.returncode
        step.stdout = safe_text(p.stdout)
        step.stderr = safe_text(p.stderr)
        step.status = "VERIFIED" if p.returncode == 0 else "FALSIFIED"
    except subprocess.TimeoutExpired as e:
        step.exit_code = 124
        step.stdout = safe_text((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        step.stderr = f"Timed out after {timeout}s"
        step.status = "UNVERIFIABLE"
    step.duration_s = round(time.monotonic() - started, 2)


def infer_repair(step: Step, repo: Path) -> str | None:
    """One conservative, evidence-based repair: resolve a missing Python script by basename."""
    m = re.search(r"\bpython3?\s+((?:[\w.-]+/)*[\w.-]+\.py)(?=\s|$)", step.command)
    if not m:
        return None
    requested = m.group(1)
    cd = re.match(r"\s*cd\s+([^;&|]+)\s*&&\s*", step.command)
    cwd = (repo / cd.group(1).strip().strip("'\"")) if cd else repo
    if (cwd / requested).is_file():
        return None
    matches = []
    for p in repo_files(repo, 100000):
        if p.name == Path(requested).name:
            matches.append(p.relative_to(repo).as_posix())
            if len(matches) > 1:
                break
    if len(matches) != 1:
        return None
    repaired_path = os.path.relpath(repo / matches[0], cwd).replace(os.sep, "/")
    return step.command[:m.start(1)] + repaired_path + step.command[m.end(1):]


def command_is_server(step: Step) -> bool:
    c = step.command.lower()
    strong = r"\b(npm run (?:dev|start)|npm start|pnpm (?:dev|start)|yarn (?:dev|start)|uvicorn\b|flask run\b|python3?\s+\S*(?:server|app)\.py\b|python3?\s+manage\.py\s+runserver|node\s+\S*(?:server|app)\.js\b|go run\b|php -s\b)"
    return step.category == "quickstart" and bool(re.search(strong, c))


def mark_manual_steps(steps: list[Step]) -> None:
    for s in steps:
        c = s.command.strip()
        if re.fullmatch(r"pip3?\s+install", c):
            s.status = "UNVERIFIABLE"
            s.reason = "incomplete install command: no package or requirements file specified"
        elif re.search(r"\b(?:browser|desktop|gui|tkinter|pyqt|pyside)\b", c, re.I) and s.category == "quickstart":
            s.status = "UNVERIFIABLE"
            s.reason = "likely interactive/GUI quickstart; headless sandbox cannot verify it safely"
        elif re.search(r"<[^>]+>", c) and not re.search(r"https?://", c):
            s.status = "UNVERIFIABLE"
            s.reason = "command contains a documentation placeholder; not safe to execute literally"


def expected_response_matches(body: str, expected: str) -> bool:
    try:
        wanted = json.loads(expected)
        actual = json.loads(body)
    except (ValueError, TypeError):
        return expected in body
    def subset(w: Any, a: Any) -> bool:
        if isinstance(w, dict) and isinstance(a, dict):
            return all(k in a and subset(v, a[k]) for k, v in w.items())
        if isinstance(w, list) and isinstance(a, list):
            return len(a) >= len(w) and all(subset(x, y) for x, y in zip(w, a))
        return w == a
    return subset(wanted, actual)


def try_endpoint_probe(name: str, url: str, timeout: int, expected: str | None = None) -> tuple[str, str]:
    # Probe only loopback from inside the sandbox, never a public URL.
    parsed = urlparse(url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return "UNVERIFIABLE", "Skipped: only localhost endpoints are probed."
    py = ("import urllib.request,urllib.error,sys; u=sys.argv[1]; t=int(sys.argv[2]); "
          "\ntry:\n r=urllib.request.urlopen(u,timeout=t); b=r.read(4096).decode('utf-8','replace'); print('HTTP',r.status); print(b); sys.exit(0 if 200<=r.status<400 else 1)"
          "\nexcept urllib.error.HTTPError as e:\n print('HTTP',e.code); print(e.read(4096).decode('utf-8','replace')); sys.exit(1)"
          "\nexcept Exception as e:\n print(type(e).__name__+': '+str(e)); sys.exit(2)")
    node = ("const u=process.argv[1], t=Number(process.argv[2]); "
            "const c=new AbortController(); const h=setTimeout(()=>c.abort(),t*1000); "
            "fetch(u,{signal:c.signal}).then(async r=>{console.log('HTTP',r.status); console.log((await r.text()).slice(0,4096)); process.exit(r.ok?0:1)})"
            ".catch(e=>{console.log(e.name+': '+e.message);process.exit(2)}).finally(()=>clearTimeout(h));")
    try:
        p = subprocess.run(["docker", "exec", name, "python3", "-c", py, url, str(timeout)], text=True, capture_output=True, timeout=timeout + 3)
        if p.returncode in {126, 127} and ("executable file not found" in (p.stderr or "").lower() or "not found" in (p.stderr or "").lower()):
            p = subprocess.run(["docker", "exec", name, "node", "-e", node, url, str(timeout)], text=True, capture_output=True, timeout=timeout + 3)
        if p.returncode == 0:
            output = safe_text(p.stdout, 4000)
            if expected:
                body = output.split("\n", 1)[1] if "\n" in output else ""
                if not expected_response_matches(body, expected):
                    return "FALSIFIED", "HTTP request succeeded, but documented response fragment did not match.\n" + output
            return "VERIFIED", output
        if p.returncode == 1:
            return "FALSIFIED", safe_text(p.stdout or p.stderr, 4000)
        return "UNVERIFIABLE", safe_text(p.stdout or p.stderr, 4000)
    except subprocess.TimeoutExpired:
        return "UNVERIFIABLE", f"Timed out after {timeout}s"


def as_report(repo_url: str, ref: str, readme: str | None, inventory_data: dict[str, Any], steps: list[Step], endpoints: list[dict[str, Any]], started: str, readmes: list[str] | None = None) -> dict[str, Any]:
    return {
        "tool": "DocDrift", "version": VERSION, "repository": repo_url, "commit": ref,
        "created_at": started, "readme": readme, "readmes_scanned": readmes or ([readme] if readme else []), "inventory": inventory_data,
        "claims": [{"id": s.id, "type": s.category, "claim": s.command, "source": {"file": s.source_file, "line": s.line, "section": s.section},
                    "status": s.status, "exit_code": s.exit_code, "duration_seconds": s.duration_s,
                    "stdout": s.stdout, "stderr": s.stderr, "blocked_reason": s.reason or None,
                    "repair_attempt": {"command": s.repair_command, "status": s.repair_status, "stdout": s.repair_stdout, "stderr": s.repair_stderr} if s.repair_command else None} for s in steps] +
                   [{"id": e["id"], "type": "endpoint", "claim": f"GET {e['url']}", "source": {"file": e.get("file", readme), "line": e["line"]}, "status": e["status"], "expected_response": e.get("expected"), "evidence": e["evidence"]} for e in endpoints]
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# DocDrift audit: `{report['repository']}`", "", f"- **Commit:** `{report['commit']}`", f"- **Created:** {report['created_at']}", f"- **README:** {report['readme'] or 'not found'}", f"- **README files scanned:** {', '.join(f'`{p}`' for p in report.get('readmes_scanned', [])) or 'none'}", "", "## Verdicts", "", "| ID | Status | Type | README claim / check | Source |", "|---|---|---|---|---|"]
    for c in report["claims"]:
        src = c.get("source", {})
        claim = str(c.get("claim", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {c['id']} | **{c['status']}** | {c.get('type','')} | `{claim}` | {src.get('file','')}:{src.get('line','')} |")
    lines += ["", "## Repository inventory", "", f"- **Stacks:** {', '.join(report['inventory']['stacks']) or 'not detected'}", f"- **Likely test files:** {', '.join(f'`{p}`' for p in report['inventory']['test_files']) or 'none detected'}", "", "## Command evidence", ""]
    for c in report["claims"]:
        if c.get("type") == "endpoint":
            expected = f" Expected response fragment: `{c['expected_response']}`." if c.get("expected_response") else ""
            lines += [f"### {c['id']} — {c['status']}: {c['claim']}", "", c.get("evidence", "") + expected, ""]
            continue
        lines += [f"### {c['id']} — {c['status']}: `{c['claim']}`", "", f"Source: `{c.get('source',{}).get('file')}:{c.get('source',{}).get('line')}` · exit `{c.get('exit_code')}` · {c.get('duration_seconds',0)}s", "", "```text", safe_text(c.get("stdout", "") + c.get("stderr", ""), 5000).rstrip() or "(no output)", "```", ""]
        repair = c.get("repair_attempt")
        if repair:
            lines += [f"**Repair retry:** `{repair['command']}` → **{repair['status']}**", "", "```text", safe_text(repair.get("stdout", "") + repair.get("stderr", ""), 5000).rstrip() or "(no output)", "```", ""]
    lines += ["---", "Generated by DocDrift. A successful command verifies that command in this environment, not every surrounding natural-language claim.", ""]
    return "\n".join(lines)


def make_sandbox_tree_writable(repo: Path) -> None:
    # The container runs as UID 1000; cloned files may be owned by another host UID.
    # This is a disposable clone, not the caller's working tree.
    for base, dirs, files in os.walk(repo):
        os.chmod(base, 0o777)
        for name in dirs + files:
            child = Path(base) / name
            if not child.is_symlink():
                try:
                    mode = child.stat().st_mode
                    os.chmod(child, (mode | 0o666) if child.is_file() else 0o777)
                except OSError:
                    pass


def write_reports(report: dict[str, Any], args: argparse.Namespace) -> None:
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"JSON report: {out}")
    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(report), encoding="utf-8")
        print(f"Markdown report: {out}")


def audit(args: argparse.Namespace) -> int:
    repo_url = normalize_github(args.repo)
    temp = Path(tempfile.mkdtemp(prefix="docdrift-"))
    repo = temp / "repo"
    created = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        print(f"\nDocDrift {VERSION} · plan and evidence, not vibes\nTarget: {repo_url.removesuffix('.git')}\n")
        print("[1/4] Cloning shallow copy…")
        try:
            p = subprocess.run(["git", "clone", "--depth", "1", "--", repo_url, str(repo)], text=True, capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            print("git clone timed out after 300 seconds.", file=sys.stderr)
            return 2
        if p.returncode:
            print(p.stderr.strip() or "git clone failed", file=sys.stderr)
            return 2
        refp = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], text=True, capture_output=True)
        ref = refp.stdout.strip() or "unknown"
        inv = inventory(repo)
        readmes = find_readmes(repo)
        readme = readmes[0] if readmes else None
        if readmes:
            print(f"[2/4] Scanning {len(readmes)} README file(s) and repository files…")
            steps, eps = [], []
            for doc in readmes:
                rel = str(doc.relative_to(repo))
                steps.extend(extract_steps(doc, rel))
                eps.extend(endpoint_claims(doc, rel))
            # Stable global IDs and de-duplication by source + exact command/URL.
            deduped, seen = [], set()
            for step in steps:
                key = (step.source_file, step.command)
                if key not in seen:
                    seen.add(key)
                    step.id = f"C{len(deduped)+1:03}"
                    deduped.append(step)
            steps = deduped
            for i, endpoint in enumerate(eps, 1):
                endpoint["id"] = f"E{i:03}"
            mark_manual_steps(steps)
        else:
            print("[2/4] No README found; documenting this as a failed documentation claim.")
            steps = [Step("D001", "A README exists in the repository", "documentation", 0, "documentation", status="FALSIFIED", exit_code=1, stderr="No README file was found.", source_file="")]
            eps = []
        print(f"[3/4] Plan: {len(steps)} claim/check item(s), {sum(s.status == 'PLANNED' and not s.risky for s in steps)} eligible command(s), {len(eps)} endpoint claim(s).")
        print(f"      Detected: {', '.join(inv['stacks']) or 'no known stack'} · likely tests: {len(inv['test_files'])} file(s)")
        if steps:
            for s in steps:
                suffix = (" [BLOCKED: " + s.reason + "]") if s.risky else (" [MANUAL: " + s.reason + "]" if s.status == "UNVERIFIABLE" else "")
                print(f"  {s.id} [{s.category:9}] {s.source_file}:{s.line} {s.command}{suffix}")
        else:
            print("  No supported fenced shell commands found.")
        for e in eps:
            print(f"  {e['id']} [endpoint ] {e.get('file','README')}:{e['line']} GET {e['url']} (probe planned)")
        if not args.execute:
            report = as_report(repo_url.removesuffix(".git"), ref, str(readme.relative_to(repo)) if readme else None, inv, steps, eps, created, [str(r.relative_to(repo)) for r in readmes])
            write_reports(report, args)
            print("\nPlan only. Use --execute to review, approve, and run commands in Docker.")
            print("Plan scope: README code/inline commands + endpoint-shaped claims. General prose claims and broad code repairs are not inferred.")
            return 0
        runnable = [s for s in steps if not s.risky and s.status == "PLANNED"]
        if not runnable:
            print("Nothing eligible to execute; writing the plan/evidence report.")
            report = as_report(repo_url.removesuffix(".git"), ref, str(readme.relative_to(repo)) if readme else None, inv, steps, eps, created, [str(r.relative_to(repo)) for r in readmes])
            write_reports(report, args)
            return 0
        if not shutil.which("docker"):
            print("Docker not found; install Docker Engine/Desktop to execute. Plan generation worked.", file=sys.stderr)
            return 2
        if not sys.stdin.isatty():
            print("Execution requires a terminal for explicit approval. Run interactively.", file=sys.stderr)
            return 2
        print("\nSAFETY REVIEW: this repository's commands are untrusted code. They run inside Docker with resource caps, no added Linux capabilities, no host mounts except the disposable clone, and network disabled by default. Docker is containment, not a perfect security boundary.")
        if args.allow_network:
            print("NETWORK: enabled for this sandbox at your request.")
        if any(s.risky for s in steps):
            print(f"SKIPPED {sum(s.risky for s in steps)} command(s) matched a deny-list pattern.")
        if input("Type RUN to proceed: ").strip() != "RUN":
            print("Cancelled.")
            return 0
        make_sandbox_tree_writable(repo)
        name = "docdrift-" + uuid.uuid4().hex[:10]
        image = select_image(repo, args.image)
        network = "bridge" if args.allow_network else "none"
        start = ["docker", "run", "-d", "--name", name, "--network", network, "--memory", "2g", "--cpus", "2", "--pids-limit", "128", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "1000:1000", "--env", "HOME=/tmp", "--env", "PIP_USER=1", "-v", f"{repo}:/workspace", "-w", "/workspace", image, "sh", "-lc", "sleep infinity"]
        p = subprocess.run(start, text=True, capture_output=True)
        if p.returncode:
            print(p.stderr.strip() or "Could not start sandbox", file=sys.stderr)
            return 2
        try:
            # Run finite setup/test commands first, then launch documented servers detached.
            finite = [s for s in runnable if not command_is_server(s) and s.category != "endpoint"]
            servers = [s for s in runnable if command_is_server(s)]
            endpoint_commands = [s for s in runnable if s.category == "endpoint"]
            aborted = False
            for i, s in enumerate(finite, 1):
                print(f"\n[{i}/{len(finite)}] $ {s.command}")
                run_container_step(name, s, args.timeout)
                print((s.stdout + s.stderr).rstrip()[-args.output_limit:] or "(no output)")
                print(f"=> {s.status} · exit {s.exit_code} · {s.duration_s:.2f}s")
                if s.status == "FALSIFIED" and args.repair:
                    candidate = infer_repair(s, repo)
                    if candidate:
                        s.repair_command = candidate
                        retry = dataclasses.replace(s, command=candidate, status="PLANNED", stdout="", stderr="", exit_code=None)
                        print(f"  ↳ Evidence-based repair candidate: {candidate}")
                        run_container_step(name, retry, args.timeout)
                        s.repair_status, s.repair_stdout, s.repair_stderr = retry.status, retry.stdout, retry.stderr
                        print(f"  ↳ Retry => {retry.status} · exit {retry.exit_code}")
                    else:
                        print("  ↳ No conservative automatic repair found; original repo left unchanged.")
                if s.status == "FALSIFIED" and args.stop_on_failure and s.repair_status != "VERIFIED":
                    for pending in finite[i:]:
                        pending.status = "UNVERIFIABLE"
                        pending.reason = "skipped because --stop-on-failure was set"
                    aborted = True
                    break
            if servers and not aborted:
                for s in servers:
                    print(f"\n[server] $ {s.command} (detached)")
                    started_server = time.monotonic()
                    p = subprocess.run(["docker", "exec", "-d", name, "sh", "-lc", s.command + " > /tmp/docdrift-server.log 2>&1"], text=True, capture_output=True)
                    s.duration_s = round(time.monotonic() - started_server, 2)
                    s.status = "UNVERIFIABLE"
                    s.exit_code = None
                    s.stdout = "Started as a detached sandbox process; endpoint availability determines its verification."
                    s.stderr = safe_text(p.stderr)
                    if p.returncode:
                        s.status = "FALSIFIED"
                        s.exit_code = p.returncode
                        s.stdout = ""
                        print(s.stderr)
                    else:
                        print("Detached process launched; waiting for the documented local endpoint(s).")
            if endpoint_commands and not aborted:
                for s in endpoint_commands:
                    print(f"\n[endpoint command] $ {s.command}")
                    if not servers:
                        s.status = "UNVERIFIABLE"
                        s.reason = "no documented server launcher was run"
                        s.stderr = "Endpoint command skipped: no documented server launcher was found."
                    else:
                        run_container_step(name, s, args.timeout)
                    print((s.stdout + s.stderr).rstrip()[-args.output_limit:] or "(no output)")
                    print(f"=> {s.status} · exit {s.exit_code}")
            if eps and servers and not aborted:
                for e in eps:
                    target = (args.base_url.rstrip("/") + urlparse(e["url"]).path) if args.base_url else e["url"]
                    e["url"] = target
                    if urlparse(target).hostname not in {"localhost", "127.0.0.1", "0.0.0.0"}:
                        e["status"] = "UNVERIFIABLE"
                        e["evidence"] = "Relative endpoint needs --base-url, or endpoint host is not loopback; no request sent."
                        print(f"\n{e['id']} GET {target} => UNVERIFIABLE\n{e['evidence']}")
                        continue
                    deadline = time.monotonic() + min(args.timeout, 15)
                    status, evidence = "UNVERIFIABLE", "Server did not become reachable before timeout."
                    while time.monotonic() < deadline:
                        status, evidence = try_endpoint_probe(name, target, min(3, args.timeout), e.get("expected"))
                        if status != "UNVERIFIABLE":
                            break
                        time.sleep(0.7)
                    e["status"], e["evidence"] = status, evidence
                    print(f"\n{e['id']} GET {target} => {status}\n{evidence}")
                    # Any HTTP response proves the server started; the endpoint itself can still fail.
                    if re.search(r"HTTP \d{3}", evidence):
                        for s in servers:
                            if s.status == "UNVERIFIABLE":
                                s.status = "VERIFIED"
                    elif status == "UNVERIFIABLE":
                        logp = subprocess.run(["docker", "exec", name, "sh", "-lc", "cat /tmp/docdrift-server.log 2>/dev/null || true"], text=True, capture_output=True)
                        server_log = safe_text(logp.stdout or logp.stderr)
                        if re.search(r"can't open file|no such file|module not found|modulenotfounderror|command not found", server_log, re.I):
                            for server_step in servers:
                                server_step.status = "FALSIFIED"
                                server_step.exit_code = 1
                                server_step.stderr = server_log
                                server_step.stdout = "Documented server command exited during startup."
                            if args.repair and servers:
                                server_step = servers[0]
                                candidate = infer_repair(server_step, repo)
                                if candidate:
                                    server_step.repair_command = candidate
                                    print(f"  ↳ Startup failure suggests path repair: {candidate}")
                                    launch = subprocess.run(["docker", "exec", "-d", name, "sh", "-lc", candidate + " > /tmp/docdrift-repair-server.log 2>&1"], text=True, capture_output=True)
                                    if launch.returncode == 0:
                                        retry_status, retry_evidence = "UNVERIFIABLE", "Server did not become reachable after repair."
                                        retry_deadline = time.monotonic() + min(args.timeout, 12)
                                        while time.monotonic() < retry_deadline:
                                            retry_status, retry_evidence = try_endpoint_probe(name, target, min(3, args.timeout), e.get("expected"))
                                            if retry_status != "UNVERIFIABLE":
                                                break
                                            time.sleep(0.7)
                                        server_step.repair_status = retry_status
                                        server_step.repair_stdout = retry_evidence
                                        server_step.repair_stderr = ""
                                        e["status"] = retry_status
                                        e["evidence"] = "After repair retry: " + retry_evidence
                                        if retry_status == "VERIFIED":
                                            print("  ↳ Repaired launch reached the endpoint successfully; original README command remains falsified.")
                                    else:
                                        server_step.repair_status = "FALSIFIED"
                                        server_step.repair_stderr = launch.stderr
                                        e["evidence"] += "\nRepair launch failed: " + launch.stderr
            elif eps:
                for e in eps:
                    e["status"] = "UNVERIFIABLE"
                    e["evidence"] = "Endpoint observed but no documented server start command was run."
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        report = as_report(repo_url.removesuffix(".git"), ref, str(readme.relative_to(repo)) if readme else None, inv, steps, eps, created, [str(r.relative_to(repo)) for r in readmes])
        write_reports(report, args)
        counts = {x: sum(c["status"] == x for c in report["claims"]) for x in ("VERIFIED", "FALSIFIED", "UNVERIFIABLE", "PLANNED")}
        print("\nAudit totals: " + " · ".join(f"{k}: {v}" for k, v in counts.items()))
        return 1 if counts["FALSIFIED"] else 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="docdrift", description="Check README claims against a clean repo checkout.", epilog="Plan-only by default. Execution requires Docker, --execute, and typing RUN.")
    p.add_argument("repo", help="Public GitHub repository URL")
    p.add_argument("--execute", action="store_true", help="Execute extracted README commands in isolated Docker")
    p.add_argument("--repair", action="store_true", help="After a failure, try one conservative inferred Python-script path correction and rerun it in the sandbox")
    p.add_argument("--allow-network", action="store_true", help="Allow sandbox egress (needed for dependency installs; increases risk)")
    p.add_argument("--image", help="Override automatically selected sandbox image")
    p.add_argument("--timeout", type=int, default=120, help="Timeout per README command in seconds (1–1800; default 120)")
    p.add_argument("--base-url", help="Explicit localhost base URL for endpoint checks, e.g. http://127.0.0.1:8000")
    p.add_argument("--report", default="docdrift-report.md", help="Markdown report path (default: docdrift-report.md)")
    p.add_argument("--json", help="Also write a machine-readable JSON report")
    p.add_argument("--output-limit", type=int, default=5000, help="Max command-output chars shown in terminal (default 5000)")
    p.add_argument("--stop-on-failure", action="store_true", help="Skip remaining commands after the first failed command")
    return p


def main() -> int:
    p = parser()
    args = p.parse_args()
    if not 1 <= args.timeout <= 1800:
        p.error("--timeout must be between 1 and 1800")
    if args.repair and not args.execute:
        p.error("--repair requires --execute")
    if args.base_url:
        base = urlparse(args.base_url)
        if base.scheme not in {"http", "https"} or base.hostname not in {"localhost", "127.0.0.1", "0.0.0.0"}:
            p.error("--base-url must be an http(s) loopback URL (localhost, 127.0.0.1, or 0.0.0.0)")
    if args.output_limit < 100:
        p.error("--output-limit must be at least 100")
    try:
        return audit(args)
    except KeyboardInterrupt:
        print("\nInterrupted. Temporary sandbox cleanup will run.", file=sys.stderr)
        return 130
    except (argparse.ArgumentTypeError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"Required tool not found: {e.filename}. Install git and retry.", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
