#!/usr/bin/env python3
"""DocDrift: audit README instructions in a disposable Docker sandbox.

Plan-only by default. Nothing from the target repository runs unless --execute is used.
"""
from __future__ import annotations
import argparse
import dataclasses
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

SUPPORTED = {"python", "python3", "pip", "pip3", "pytest", "uv", "poetry", "npm", "npx", "pnpm", "yarn", "node", "make", "cargo", "go"}
SKIP_WORDS = ("--", "#", "```", "http://", "https://")

@dataclasses.dataclass
class Step:
    command: str
    section: str
    line: int
    category: str


def github_url(value: str) -> str:
    value = value.strip().rstrip("/")
    m = re.fullmatch(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?", value)
    if not m:
        raise argparse.ArgumentTypeError("Expected a GitHub repo URL like https://github.com/owner/repo")
    return f"https://github.com/{m.group(1)}/{m.group(2)}.git"


def command_category(command: str, section: str) -> str:
    s = (command + " " + section).lower()
    if any(w in s for w in ("test", "pytest", "unittest", "vitest", "jest")):
        return "tests"
    if any(w in s for w in ("install", "requirements", "dependency", "dependencies")):
        return "install"
    if any(w in s for w in ("quickstart", "getting started", "run", "start", "launch", "usage")):
        return "quickstart"
    return "command"


def readme_candidates(repo: Path) -> list[Path]:
    candidates = [repo / "README.md", repo / "README.rst", repo / "README", repo / "readme.md"]
    return [p for p in candidates if p.is_file()]


def extract_steps(readme: Path) -> list[Step]:
    text = readme.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    steps: list[Step] = []
    heading = "README"
    in_fence = False
    fence_lang = ""
    buf: list[tuple[int, str]] = []
    for n, line in enumerate(lines, 1):
        h = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if h and not in_fence:
            heading = h.group(1).strip()
        fence = re.match(r"^\s*(```+|~~~+)\s*([\w+-]*)", line)
        if fence:
            if not in_fence:
                in_fence = True
                fence_lang = fence.group(2).lower()
                buf = []
            else:
                if fence_lang in ("", "sh", "shell", "bash", "zsh", "console", "terminal", "cmd", "powershell"):
                    for lineno, raw in buf:
                        cmd = raw.strip()
                        cmd = re.sub(r"^\$\s*", "", cmd)
                        # Console transcripts often prefix the command with a prompt.
                        cmd = re.sub(r"^>>>\s*", "", cmd)
                        if not cmd or cmd.startswith(SKIP_WORDS):
                            continue
                        # Only attempt recognizable developer commands. This avoids treating
                        # arbitrary README examples as runnable instructions by default.
                        first = re.split(r"\s+|&&|[|;]", cmd, maxsplit=1)[0]
                        if first not in SUPPORTED:
                            continue
                        # Server launchers can wait forever; endpoint/server checks belong
                        # in a later runner milestone rather than this first CLI cut.
                        if re.search(r"\b(runserver|serve|dev|start)\b", cmd.lower()) and not re.search(r"\b(test|check)\b", cmd.lower()):
                            continue
                        steps.append(Step(cmd, heading, lineno, command_category(cmd, heading)))
                in_fence = False
                fence_lang = ""
            continue
        if in_fence:
            buf.append((n, line))
    dedup: list[Step] = []
    seen = set()
    for s in steps:
        key = s.command
        if key not in seen:
            seen.add(key)
            dedup.append(s)
    return dedup


def choose_image(repo: Path, override: str | None) -> str:
    if override:
        return override
    if (repo / "package.json").exists():
        return "node:22-bookworm-slim"
    if (repo / "pyproject.toml").exists() or (repo / "requirements.txt").exists() or list(repo.glob("*.py")):
        return "python:3.12-slim"
    return "ubuntu:24.04"


def run(args: argparse.Namespace) -> int:
    url = github_url(args.repo)
    root = Path(tempfile.mkdtemp(prefix="docdrift-"))
    repo = root / "repo"
    try:
        print(f"\n  DocDrift · README audit\n  Target: {url.removesuffix('.git')}\n")
        print("[1/3] Cloning repository (shallow)…")
        p = subprocess.run(["git", "clone", "--depth", "1", "--", url, str(repo)], text=True, capture_output=True)
        if p.returncode:
            print(p.stderr.strip() or "git clone failed", file=sys.stderr)
            return 2
        readmes = readme_candidates(repo)
        if not readmes:
            print("No README found at the repository root. Verdict: UNVERIFIABLE")
            return 0
        readme = readmes[0]
        print(f"[2/3] Reading {readme.name}…")
        steps = extract_steps(readme)
        print(f"      Found {len(steps)} runnable command(s) in fenced shell blocks.\n")
        if not steps:
            print("No supported commands found. This first version extracts Python, Node, Go, Rust, and make commands from fenced README examples.")
            return 0
        print("Execution plan (review before execution):")
        for i, s in enumerate(steps, 1):
            print(f"  {i:>2}. [{s.category.upper():10}] {s.command}\n      README:{s.line} · {s.section}")
        print("\nNot included yet: prose-only claims, endpoint probing, install-to-test environment inference, and AI-generated repair patches.")
        if not args.execute:
            print("\nPlan only. To execute these commands in Docker, re-run with --execute.")
            return 0
        if not shutil.which("docker"):
            print("Docker not found. Install Docker Desktop/Engine, then retry.", file=sys.stderr)
            return 2
        if not sys.stdin.isatty():
            print("Execution requires an interactive confirmation. Run this command in a terminal.", file=sys.stderr)
            return 2
        print("\nSAFETY: README commands are untrusted code. They will run inside a disposable Docker container, with no host filesystem mounted except this temporary clone. Network is disabled by default.")
        answer = input("Type RUN to execute the plan: ").strip()
        if answer != "RUN":
            print("Cancelled; nothing was executed.")
            return 0
        image = choose_image(repo, args.image)
        name = "docdrift-" + uuid.uuid4().hex[:10]
        network = "bridge" if args.allow_network else "none"
        os.chmod(repo, 0o777)
        print(f"\n[3/3] Starting disposable sandbox ({image}, network={network})…")
        start = ["docker", "run", "-d", "--name", name, "--network", network,
                 "--memory", "2g", "--cpus", "2", "--pids-limit", "128",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                 "-v", f"{repo}:/workspace", "-w", "/workspace", image, "sleep", "infinity"]
        p = subprocess.run(start, text=True, capture_output=True)
        if p.returncode:
            print(p.stderr.strip() or "Could not start Docker container", file=sys.stderr)
            return 2
        results = []
        try:
            for i, step in enumerate(steps, 1):
                print(f"\n[{i}/{len(steps)}] $ {step.command}")
                try:
                    result = subprocess.run(["docker", "exec", name, "sh", "-lc", step.command], text=True,
                                            capture_output=True, timeout=args.timeout)
                    out = (result.stdout + result.stderr).strip()
                    status = "VERIFIED" if result.returncode == 0 else "FALSIFIED"
                    code = result.returncode
                except subprocess.TimeoutExpired as e:
                    out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
                    out += f"\nTimed out after {args.timeout}s"
                    status, code = "UNVERIFIABLE", 124
                print((out or "(no output)")[-5000:])
                print(f"=> {status} (exit {code})")
                results.append((step, status, code))
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        print("\nAudit summary:")
        for status in ("VERIFIED", "FALSIFIED", "UNVERIFIABLE"):
            print(f"  {status:12} {sum(1 for _, s, _ in results if s == status)}")
        print("\nEvidence above is command output from a disposable container. This MVP executes only extracted fenced commands; it does not yet prove broader natural-language claims.")
        return 0 if all(s == "VERIFIED" for _, s, _ in results) else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether a GitHub repo's README commands work.")
    parser.add_argument("repo", help="Public GitHub repository URL")
    parser.add_argument("--execute", action="store_true", help="Run extracted README commands in disposable Docker (otherwise plan only)")
    parser.add_argument("--allow-network", action="store_true", help="Allow sandbox network access (needed for dependency installs; expands risk)")
    parser.add_argument("--image", help="Override auto-selected Docker image")
    parser.add_argument("--timeout", type=int, default=120, help="Per-command timeout in seconds (default: 120)")
    args = parser.parse_args()
    if args.timeout < 1 or args.timeout > 1800:
        parser.error("--timeout must be between 1 and 1800 seconds")
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except FileNotFoundError as e:
        print(f"Missing required tool: {e.filename}. Install git and retry.", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
