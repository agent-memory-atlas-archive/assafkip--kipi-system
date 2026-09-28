#!/usr/bin/env python3
"""notes-publish: copy session notes and RCAs onto a notes-only `kipi/notes` branch.

ASK-2190 (plan: q-system/output/plans/notes-publish-2026-09-28.md). Cloud
sessions see only GitHub. The Stop hook `q-system/hooks/auto-commit.py` commits
notes on whatever branch is checked out and never pushes, so an instance's
`main` handoff went months stale while the fresh one sat on a feature branch,
and RCAs (under the ignored `q-system/output/**`) never reached git at all.

What it does, with git PLUMBING only (temp GIT_INDEX_FILE, hash-object -w,
update-index --cacheinfo, write-tree, commit-tree, push <sha>:refs/heads/kipi/notes):
  - it never checks out, and never touches HEAD, the index, the working tree,
    the checked-out branch or main; it never pushes code;
  - only allowlisted notes paths (`is_notes_path`) are ever added to the tree,
    mirroring chief/instance.py STATE_FILES / HANDOFF / CASE_FILES / ACTIVE_CASE
    plus `output/rca/*.md`;
  - an origin that is PUBLIC gets nothing, and so does one whose visibility
    cannot be proven (fail closed): unauthenticated GET https://github.com/<o>/<r>,
    200 public, 404 private, anything else unknown;
  - in the skeleton (instance-registry.json at the root, the same self-detection
    as instance-automation-guard) the repo is public, so its RCAs go to the
    consulting checkout's `kipi/notes` under `kipi-system/output/rca/` and its
    own handoff is published nowhere (founder choice b, 2026-09-28);
  - no change -> no commit; a rejected push (race) is refetched, rebuilt and
    retried once, then logged and dropped. Commits end `[skip ci]`.

Called non-fatally at the end of auto-commit.py's main(). Always exits 0; every
outcome is one `notes-publish:` line on STDOUT (the fleet wiring discards the
hook's stderr). Test seam: KIPI_NOTES_VISIBILITY=public|private|unknown replaces
the network check. stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BRANCH = "kipi/notes"
REF = "refs/heads/" + BRANCH
CONSULTING = "ASK_AI_consultant"
SKELETON_PREFIX = "kipi-system/output/rca/"
NET_TIMEOUT = 30          # per network git call; the hook caps the whole run
HTTP_TIMEOUT = 5

# Mirrors chief/instance.py: HANDOFF, STATE_FILES, ACTIVE_CASE, CASE_FILES.
HANDOFF = "memory/last-handoff.md"
STATE_FILES = ("dashboard.md", "memory/investigation-state.md")
ACTIVE_CASE = ".active-case"
CASE_FILES = ("memory/last-handoff.md", "memory/investigation-state.md")

_NOTES_RE = re.compile(
    r"^(?:q-[^/]+/(?:memory/last-handoff\.md|dashboard\.md|memory/investigation-state\.md"
    r"|\.active-case|investigations/case-[^/]+/memory/(?:last-handoff|investigation-state)\.md"
    r"|output/rca/[^/]+\.md)"
    r"|kipi-system/output/rca/[^/]+\.md)$")

_GH_RE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([^/]+)/([^/]+?)(?:\.git)?/?$")


def is_notes_path(path: str) -> bool:
    """The single allowlist. Nothing outside it is ever added to kipi/notes."""
    if any(part in ("", ".", "..") for part in path.split("/")):
        return False
    return bool(_NOTES_RE.match(path))


def parse_github(url: str):
    m = _GH_RE.match(url.strip())
    return (m.group(1), m.group(2)) if m else None


def visibility_of(url: str, opener=urllib.request.urlopen) -> str:
    """'public' | 'private' | 'unknown'. Unknown means publish nothing."""
    gh = parse_github(url)
    if gh is None:
        return "unknown"
    req = urllib.request.Request(f"https://github.com/{gh[0]}/{gh[1]}", method="GET")
    try:
        with opener(req, timeout=HTTP_TIMEOUT) as resp:
            status = getattr(resp, "status", None)
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception:
        return "unknown"
    return {200: "public", 404: "private"}.get(status, "unknown")


def _default_visibility(url: str) -> str:
    forced = os.environ.get("KIPI_NOTES_VISIBILITY", "").strip()
    if forced in ("public", "private", "unknown"):
        return forced
    return visibility_of(url)


def _plain_file(p: Path) -> bool:
    return p.is_file() and not p.is_symlink()


def collect_instance(root: Path) -> dict:
    """tree path -> source file, for every q-* dir (q-system included)."""
    out = {}
    for q in sorted(root.glob("q-*")):
        if not q.is_dir() or q.is_symlink():
            continue
        cands = [q / HANDOFF, q / ACTIVE_CASE] + [q / r for r in STATE_FILES]
        cands += [c / r for c in sorted((q / "investigations").glob("case-*")) for r in CASE_FILES]
        cands += sorted((q / "output" / "rca").glob("*.md"))
        for p in cands:
            if _plain_file(p):
                out[p.relative_to(root).as_posix()] = p
    return out


def collect_skeleton_rcas(root: Path) -> dict:
    out = {}
    for p in sorted((root / "q-system" / "output" / "rca").glob("*.md")):
        if _plain_file(p):
            out[SKELETON_PREFIX + p.name] = p
    return out


def is_skeleton(root: Path) -> bool:
    return (root / "instance-registry.json").exists()


def consulting_checkout(root: Path):
    try:
        reg = json.loads((root / "instance-registry.json").read_text())
    except (OSError, ValueError) as e:
        return None, f"could not read instance-registry.json ({e})"
    for inst in reg.get("instances", []) if isinstance(reg, dict) else []:
        if isinstance(inst, dict) and inst.get("name") == CONSULTING:
            path = Path(os.path.expanduser(str(inst.get("path", ""))))
            if (path / ".git").exists():
                return path, None
            return None, f"consulting checkout not found at {path}"
    return None, f"no {CONSULTING} entry in instance-registry.json"


class _Git:
    def __init__(self, repo: Path, index: str | None = None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
        env["GIT_TERMINAL_PROMPT"] = "0"
        if index:
            env["GIT_INDEX_FILE"] = index
        self.repo, self.env = repo, env

    def __call__(self, *args, timeout=NET_TIMEOUT):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True, timeout=timeout)


def _last(stderr: str) -> str:
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "no error text"


def _build_and_push(g: _Git, files: dict, label: str):
    """One attempt. Returns (done, line); done=False means retryable push rejection."""
    r = g("ls-remote", "origin", REF)
    if r.returncode != 0:
        return True, f"{label}: could not reach origin ({_last(r.stderr)}); publishing nothing"
    parent = r.stdout.split()[0] if r.stdout.strip() else None
    if parent:
        r = g("fetch", "-q", "origin", f"+{REF}:refs/remotes/origin/{BRANCH}")
        if r.returncode != 0:
            return True, f"{label}: could not fetch {BRANCH} ({_last(r.stderr)}); publishing nothing"

    tmp = tempfile.mkdtemp(prefix="notes-publish-")
    try:
        gi = _Git(g.repo, index=os.path.join(tmp, "index"))
        r = gi("read-tree", parent) if parent else gi("read-tree", "--empty")
        if r.returncode != 0:
            return True, f"{label}: read-tree failed ({_last(r.stderr)})"
        for path, src in sorted(files.items()):
            if not is_notes_path(path):          # belt and braces: never code
                continue
            h = gi("hash-object", "-w", "--no-filters", str(src))
            if h.returncode != 0:
                return True, f"{label}: hash-object failed for {path} ({_last(h.stderr)})"
            u = gi("update-index", "--add", "--cacheinfo", f"100644,{h.stdout.strip()},{path}")
            if u.returncode != 0:
                return True, f"{label}: update-index failed for {path} ({_last(u.stderr)})"
        tree = gi("write-tree").stdout.strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not tree:
        return True, f"{label}: write-tree produced nothing"
    if parent and g("rev-parse", f"{parent}^{{tree}}").stdout.strip() == tree:
        return True, f"{label}: no change since {parent[:9]}; nothing to publish"

    env_id = {}
    if not g("config", "user.email").stdout.strip():
        env_id = {"GIT_AUTHOR_NAME": "kipi-notes", "GIT_AUTHOR_EMAIL": "kipi-notes@localhost",
                  "GIT_COMMITTER_NAME": "kipi-notes", "GIT_COMMITTER_EMAIL": "kipi-notes@localhost"}
    g.env.update(env_id)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    args = ["commit-tree", tree, "-m", f"notes: session notes {stamp} [skip ci]"]
    if parent:
        args[2:2] = ["-p", parent]
    c = g(*args)
    if c.returncode != 0:
        return True, f"{label}: commit-tree failed ({_last(c.stderr)})"
    sha = c.stdout.strip()
    p = g("push", "-q", "origin", f"{sha}:{REF}")
    if p.returncode != 0:
        return False, _last(p.stderr)
    return True, f"{label}: published {len(files)} file(s) to origin {BRANCH} ({sha[:9]})"


def publish_to(target: Path, files: dict, visibility, label: str) -> list:
    if not files:
        return [f"{label}: no notes files; nothing to publish"]
    g = _Git(target)
    r = g("remote", "get-url", "origin", timeout=10)
    if r.returncode != 0 or not r.stdout.strip():
        return [f"{label}: no origin remote; publishing nothing"]
    url = r.stdout.strip()
    vis = visibility(url)
    if vis == "public":
        return [f"{label}: origin is public; publishing nothing to it"]
    if vis != "private":
        return [f"{label}: origin visibility unknown ({vis}); publishing nothing"]
    lines = []
    for attempt in (1, 2):
        done, line = _build_and_push(g, files, label)
        if done:
            return lines + [line]
        if attempt == 1:
            lines.append(f"{label}: push rejected ({line}); refetching and retrying once")
        else:
            lines.append(f"{label}: push failed twice ({line}); stopping")
    return lines


def run(repo: str, visibility=None) -> list:
    visibility = visibility or _default_visibility
    root = Path(repo).resolve()
    if not (root / ".git").exists():
        return ["notes-publish: not a git checkout; nothing to publish"]
    if is_skeleton(root):
        rcas = collect_skeleton_rcas(root)
        if not rcas:
            return ["notes-publish: skeleton has no RCAs; nothing to publish"]
        target, why = consulting_checkout(root)
        if target is None:
            return [f"notes-publish: skeleton RCAs not published: {why}"]
        return publish_to(target, rcas, visibility, "notes-publish (skeleton RCAs -> consulting)")
    return publish_to(root, collect_instance(root), visibility, "notes-publish")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    args = ap.parse_args(argv)
    try:
        for line in run(args.repo):
            print(line)
    except Exception as e:  # never fatal: the caller is a Stop hook
        print(f"notes-publish: error: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
