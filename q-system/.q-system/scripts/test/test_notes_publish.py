#!/usr/bin/env python3
"""notes-publish.py: session notes and RCAs reach GitHub on a notes-only branch.

ASK-2190 (plan: q-system/output/plans/notes-publish-2026-09-28.md). The Stop
hook `q-system/hooks/auto-commit.py` commits notes on whatever branch is checked
out and never pushes, so a cloud session (which sees only GitHub) reads a stale
handoff. notes-publish copies ONLY notes files onto `kipi/notes` on the repo's
own origin with git plumbing and never touches HEAD, the index, the working tree
or the checked-out branch.

Every repo here is a throwaway under tmp_path with a `git init --bare` remote.
Visibility is injected (KIPI_NOTES_VISIBILITY / a checker function); nothing in
this file reaches github.com. Fixture content is generic placeholder text only:
kipi-system is a public repo.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "notes-publish.py"
HOOK = HERE.parents[2] / "hooks" / "auto-commit.py"
BRANCH = "refs/heads/kipi/notes"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path_factory, monkeypatch):
    # auto-commit's notify cache must never be the real ~/.cache/kipi.
    monkeypatch.setenv("KIPI_CACHE_HOME", str(tmp_path_factory.mktemp("cache")))
    monkeypatch.delenv("KIPI_NOTES_VISIBILITY", raising=False)
    for k in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        monkeypatch.delenv(k, raising=False)


def git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args} failed: {r.stderr}")
    return r


def make_repo(tmp_path, name="inst"):
    bare = tmp_path / f"{name}-remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    root = tmp_path / name
    root.mkdir()
    git(root, "init", "-q", "-b", "work")
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "remote", "add", "origin", str(bare))
    (root / ".gitignore").write_text("q-*/output/**\n")
    (root / "README.md").write_text("placeholder\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    return root, bare


def write(root, rel, body="placeholder\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def remote_tip(bare):
    r = git(bare, "rev-parse", "--verify", "-q", BRANCH, check=False)
    return r.stdout.strip() or None


def remote_tree(bare):
    return sorted(git(bare, "ls-tree", "-r", "--name-only", BRANCH).stdout.split())


def commit_count(bare):
    return int(git(bare, "rev-list", "--count", BRANCH).stdout.strip())


def load():
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    spec = importlib.util.spec_from_file_location("notes_publish", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def publish(root, vis="private"):
    """Run the script as the hook would, visibility injected."""
    env = dict(os.environ, KIPI_NOTES_VISIBILITY=vis)
    return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(root)],
                          capture_output=True, text=True, env=env, timeout=60)


def instance_notes(root):
    write(root, "q-ps/memory/last-handoff.md", "handoff v1\n")
    write(root, "q-ps/dashboard.md", "dashboard\n")
    write(root, "q-ps/memory/investigation-state.md", "state\n")
    write(root, "q-ps/.active-case", "case-001\n")
    write(root, "q-ps/investigations/case-001/memory/last-handoff.md", "case handoff\n")
    write(root, "q-ps/investigations/case-001/memory/investigation-state.md", "case state\n")
    write(root, "q-ps/output/rca/rca-sample-2026-09-28.md", "# RCA\n")


EXPECTED = sorted([
    "q-ps/memory/last-handoff.md",
    "q-ps/dashboard.md",
    "q-ps/memory/investigation-state.md",
    "q-ps/.active-case",
    "q-ps/investigations/case-001/memory/last-handoff.md",
    "q-ps/investigations/case-001/memory/investigation-state.md",
    "q-ps/output/rca/rca-sample-2026-09-28.md",
])


def snapshot(root):
    """Everything the publish must leave byte-identical."""
    files = {}
    for p in sorted(root.rglob("*")):
        if ".git" in p.relative_to(root).parts or not p.is_file():
            continue
        files[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return {
        "HEAD": (root / ".git" / "HEAD").read_bytes(),
        "head_sha": git(root, "rev-parse", "HEAD").stdout,
        "branch": git(root, "symbolic-ref", "HEAD").stdout,
        "index": (root / ".git" / "index").read_bytes(),
        "local_branches": git(root, "for-each-ref", "refs/heads").stdout,
        "status": git(root, "status", "--porcelain", "--ignored").stdout,
        "files": files,
    }


# --- the reproducer: a session end publishes the handoff ------------------

def test_session_end_publishes_the_handoff_to_kipi_notes(tmp_path):
    """RED on the old code: auto-commit never pushes, so no kipi/notes exists."""
    root, bare = make_repo(tmp_path)
    instance_notes(root)
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(root), KIPI_NOTES_VISIBILITY="private")
    r = subprocess.run([sys.executable, str(HOOK)], cwd=root, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert remote_tip(bare), f"no kipi/notes on the remote\n{r.stdout}\n{r.stderr}"
    assert "q-ps/memory/last-handoff.md" in remote_tree(bare)
    assert git(bare, "show", f"{BRANCH}:q-ps/memory/last-handoff.md").stdout == "handoff v1\n"


def test_auto_commit_survives_a_broken_publish(tmp_path):
    """Non-fatal: an unreachable origin must not stop the hook or its commit."""
    root, _ = make_repo(tmp_path)
    git(root, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    write(root, "q-system/memory/last-handoff.md", "v1\n")
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(root), KIPI_NOTES_VISIBILITY="private")
    r = subprocess.run([sys.executable, str(HOOK)], cwd=root, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0
    assert "q-system/memory/last-handoff.md" in git(root, "ls-files").stdout
    assert "auto-commit: done" in r.stdout


def test_auto_commit_calls_it_with_a_timeout():
    src = HOOK.read_text()
    assert "notes-publish.py" in src
    assert "timeout=" in src[src.index("def publish_notes"):]


# --- one commit, only notes, nothing local moved --------------------------

def test_one_commit_with_only_notes_and_the_checkout_untouched(tmp_path):
    root, bare = make_repo(tmp_path)
    instance_notes(root)
    before = snapshot(root)
    r = publish(root)
    assert r.returncode == 0, r.stderr
    assert remote_tree(bare) == EXPECTED, r.stdout
    assert commit_count(bare) == 1
    msg = git(bare, "log", "-1", "--format=%B", BRANCH).stdout
    assert "[skip ci]" in msg
    assert snapshot(root) == before


def test_second_run_without_change_makes_no_commit(tmp_path):
    root, bare = make_repo(tmp_path)
    instance_notes(root)
    publish(root)
    first = remote_tip(bare)
    r = publish(root)
    assert r.returncode == 0
    assert remote_tip(bare) == first
    assert "no change" in r.stdout
    write(root, "q-ps/memory/last-handoff.md", "handoff v2\n")
    publish(root)
    assert commit_count(bare) == 2
    assert git(bare, "show", f"{BRANCH}:q-ps/memory/last-handoff.md").stdout == "handoff v2\n"


def test_code_never_lands_in_kipi_notes(tmp_path):
    """Negative control: only the allowlisted notes paths are ever added."""
    root, bare = make_repo(tmp_path)
    instance_notes(root)
    write(root, "q-ps/pipeline/code.py", "print('x')\n")
    write(root, "q-ps/output/rca/evil.py", "print('x')\n")
    write(root, "q-ps/memory/other-notes.md", "x\n")
    write(root, "src/app.js", "x\n")
    git(root, "add", "q-ps/pipeline/code.py", "src/app.js")
    git(root, "commit", "-q", "-m", "code")
    publish(root)
    tree = remote_tree(bare)
    assert tree == EXPECTED
    assert not any(p.endswith((".py", ".js")) for p in tree)


def test_the_allowlist_itself_refuses_code_paths():
    mod = load()
    assert mod.is_notes_path("q-ps/memory/last-handoff.md")
    assert mod.is_notes_path("q-ps/output/rca/rca-x.md")
    assert mod.is_notes_path("kipi-system/output/rca/rca-x.md")
    for bad in ("q-ps/pipeline/code.py", "q-ps/output/rca/evil.py",
                "q-ps/output/rca/sub/x.md", "src/app.js", "q-ps/memory/x.md",
                "../q-ps/memory/last-handoff.md"):
        assert not mod.is_notes_path(bad), bad


# --- visibility: fail closed -----------------------------------------------

def test_public_origin_gets_nothing(tmp_path):
    root, bare = make_repo(tmp_path)
    instance_notes(root)
    r = publish(root, vis="public")
    assert remote_tip(bare) is None
    assert "public" in r.stdout


def test_unknown_visibility_pushes_nothing_and_says_why(tmp_path):
    root, bare = make_repo(tmp_path)
    instance_notes(root)
    r = publish(root, vis="unknown")
    assert remote_tip(bare) is None
    assert "visibility unknown" in r.stdout


def test_a_local_path_origin_is_unknown_without_injection(tmp_path, monkeypatch):
    """No override: a non-GitHub origin cannot be proven private, so nothing goes."""
    mod = load()
    root, bare = make_repo(tmp_path)
    instance_notes(root)
    called = []
    lines = mod.run(str(root), visibility=lambda url: called.append(url) or mod.visibility_of(url))
    assert remote_tip(bare) is None
    assert any("visibility unknown" in ln for ln in lines)


def test_github_url_parsing_and_status_mapping():
    mod = load()
    for url in ("https://github.com/o/r", "https://github.com/o/r.git",
                "git@github.com:o/r.git", "ssh://git@github.com/o/r.git",
                "https://x-access-token:abc@github.com/o/r.git"):
        assert mod.parse_github(url) == ("o", "r"), url
    assert mod.parse_github("/tmp/x.git") is None
    assert mod.parse_github("https://gitlab.com/o/r") is None

    class Resp:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.error

    def opener_for(outcome):
        def opener(req, timeout):
            assert req.full_url == "https://github.com/o/r"
            assert timeout <= 10
            if isinstance(outcome, int) and outcome >= 400:
                raise urllib.error.HTTPError(req.full_url, outcome, "x", {}, None)
            if isinstance(outcome, Exception):
                raise outcome
            return Resp(outcome)
        return opener

    url = "git@github.com:o/r.git"
    assert mod.visibility_of(url, opener=opener_for(200)) == "public"
    assert mod.visibility_of(url, opener=opener_for(404)) == "private"
    assert mod.visibility_of(url, opener=opener_for(429)) == "unknown"
    assert mod.visibility_of(url, opener=opener_for(OSError("no net"))) == "unknown"
    assert mod.visibility_of("/local/path.git", opener=opener_for(200)) == "unknown"


# --- choice b: the public skeleton routes RCAs to consulting ----------------

def test_skeleton_rcas_land_in_consulting_and_nothing_in_the_skeleton(tmp_path):
    mod = load()
    skel, skel_bare = make_repo(tmp_path, "skel")
    cons, cons_bare = make_repo(tmp_path, "cons")
    (skel / "instance-registry.json").write_text(json.dumps({
        "skeleton": {"name": "kipi-system"},
        "instances": [{"name": "ASK_AI_consultant", "path": str(cons)}],
    }))
    write(skel, "q-system/memory/last-handoff.md", "skeleton handoff\n")
    write(skel, "q-system/output/rca/rca-skel-2026-09-28.md", "# skeleton RCA\n")
    write(skel, "q-system/output/rca/notes.py", "x\n")
    cons_before = snapshot(cons)

    def vis(url):
        return "public" if url == str(skel_bare) else "private"

    lines = mod.run(str(skel), visibility=vis)
    assert remote_tip(skel_bare) is None, lines
    assert remote_tree(cons_bare) == ["kipi-system/output/rca/rca-skel-2026-09-28.md"], lines
    assert snapshot(cons) == cons_before

    # And a consulting origin that is not provably private gets nothing either.
    cons2, cons2_bare = make_repo(tmp_path, "cons2")
    (skel / "instance-registry.json").write_text(json.dumps({
        "instances": [{"name": "ASK_AI_consultant", "path": str(cons2)}]}))
    lines = mod.run(str(skel), visibility=lambda url: "unknown")
    assert remote_tip(cons2_bare) is None
    assert any("visibility unknown" in ln for ln in lines)


def test_skeleton_without_a_consulting_checkout_publishes_nothing(tmp_path):
    mod = load()
    skel, skel_bare = make_repo(tmp_path, "skel")
    (skel / "instance-registry.json").write_text(json.dumps({
        "instances": [{"name": "ASK_AI_consultant", "path": str(tmp_path / "absent")}]}))
    write(skel, "q-system/output/rca/rca-skel-2026-09-28.md", "# RCA\n")
    lines = mod.run(str(skel), visibility=lambda url: "private")
    assert remote_tip(skel_bare) is None
    assert any("consulting" in ln for ln in lines)


# --- race: a rejected push is rebuilt and retried once ---------------------

def test_a_rejected_push_is_retried_once(tmp_path):
    root, bare = make_repo(tmp_path)
    instance_notes(root)
    marker = tmp_path / "rejected-once"
    hook = bare / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\nif [ ! -e '{marker}' ]; then touch '{marker}'; "
                    "echo 'simulated race' >&2; exit 1; fi\nexit 0\n")
    hook.chmod(0o755)
    r = publish(root)
    assert marker.exists()
    assert remote_tip(bare), r.stdout + r.stderr
    assert "retry" in r.stdout


def test_a_push_rejected_twice_stops_and_logs(tmp_path):
    root, bare = make_repo(tmp_path)
    instance_notes(root)
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'always rejected' >&2\nexit 1\n")
    hook.chmod(0o755)
    r = publish(root)
    assert r.returncode == 0
    assert remote_tip(bare) is None
    assert "push failed" in r.stdout


# --- the consumer: a remote session overlays newer notes (codex major, #464) ---
#
# Publishing alone left kipi/notes with no reader in this repo: a cloud session
# opened on an instance repo still loaded the stale default-branch handoff.
# `--overlay` is that reader; session-start.py calls it before load_handoff().

HANDOFF_PATH = "q-ps/memory/last-handoff.md"


def overlay(root, remote=True):
    """Run the overlay as the SessionStart hook would, remote detection injected."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE_REMOTE")}
    if remote:
        env["CLAUDE_CODE_REMOTE"] = "true"
    return subprocess.run([sys.executable, str(SCRIPT), "--overlay", "--repo", str(root)],
                          capture_output=True, text=True, env=env, timeout=60)


def commit_at(root, when, msg, *paths):
    git(root, "add", "-f", *paths)
    env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    r = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def push_notes_at(tmp_path, bare, when, files):
    """Put `files` on the remote kipi/notes as one commit dated `when`."""
    pub = tmp_path / "publisher"
    git(tmp_path, "clone", "-q", str(bare), str(pub))
    git(pub, "config", "user.email", "t@t.t")
    git(pub, "config", "user.name", "t")
    git(pub, "config", "commit.gpgsign", "false")
    git(pub, "checkout", "-q", "--orphan", "kipi/notes")
    git(pub, "rm", "-rq", "--cached", ".", check=False)
    for rel, body in files.items():
        write(pub, rel, body)
    commit_at(pub, when, "notes [skip ci]", *files)
    git(pub, "push", "-q", "origin", "HEAD:refs/heads/kipi/notes")


def cloud_clone(tmp_path, bare, when_main):
    """An instance whose default branch carries an OLD handoff, pushed, then cloned."""
    src, _ = make_repo(tmp_path, "src")
    git(src, "remote", "set-url", "origin", str(bare))
    write(src, HANDOFF_PATH, "stale handoff\n")
    commit_at(src, when_main, "old handoff", HANDOFF_PATH)
    git(src, "push", "-q", "origin", "work")
    clone = tmp_path / "cloud"
    git(tmp_path, "clone", "-q", "-b", "work", str(bare), str(clone))
    return clone


def test_remote_session_overlays_a_newer_handoff_from_kipi_notes(tmp_path):
    """RED on the old code: nothing in this repo reads kipi/notes."""
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    clone = cloud_clone(tmp_path, bare, "2026-01-01T00:00:00+0000")
    push_notes_at(tmp_path, bare, "2026-09-01T00:00:00+0000",
                  {HANDOFF_PATH: "fresh handoff\n",
                   "q-ps/output/rca/rca-x-2026-09-01.md": "# RCA\n"})
    index_before = (clone / ".git" / "index").read_bytes()
    head_before = git(clone, "rev-parse", "HEAD").stdout
    r = overlay(clone)
    assert r.returncode == 0, r.stderr
    assert (clone / HANDOFF_PATH).read_text() == "fresh handoff\n", r.stdout
    assert (clone / "q-ps/output/rca/rca-x-2026-09-01.md").read_text() == "# RCA\n"
    out = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(out) == 1 and "overlaid 2" in out[0] and HANDOFF_PATH in out[0], r.stdout
    # never stages, never commits, never moves HEAD
    assert (clone / ".git" / "index").read_bytes() == index_before
    assert git(clone, "rev-parse", "HEAD").stdout == head_before


def test_a_local_handoff_newer_than_kipi_notes_is_kept(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    clone = cloud_clone(tmp_path, bare, "2026-09-20T00:00:00+0000")
    push_notes_at(tmp_path, bare, "2026-09-01T00:00:00+0000", {HANDOFF_PATH: "older notes\n"})
    r = overlay(clone)
    assert r.returncode == 0, r.stderr
    assert (clone / HANDOFF_PATH).read_text() == "stale handoff\n", r.stdout
    assert "nothing newer" in r.stdout


def test_uncommitted_local_edits_are_never_overwritten(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    clone = cloud_clone(tmp_path, bare, "2026-01-01T00:00:00+0000")
    push_notes_at(tmp_path, bare, "2026-09-01T00:00:00+0000", {HANDOFF_PATH: "fresh\n"})
    write(clone, HANDOFF_PATH, "work in progress\n")
    overlay(clone)
    assert (clone / HANDOFF_PATH).read_text() == "work in progress\n"


def test_non_remote_session_is_a_no_op(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    clone = cloud_clone(tmp_path, bare, "2026-01-01T00:00:00+0000")
    push_notes_at(tmp_path, bare, "2026-09-01T00:00:00+0000", {HANDOFF_PATH: "fresh\n"})
    r = overlay(clone, remote=False)
    assert r.returncode == 0
    assert (clone / HANDOFF_PATH).read_text() == "stale handoff\n"
    assert r.stdout.strip() == ""


def test_overlay_never_writes_a_code_file_from_the_branch(tmp_path):
    """Negative control: a code path on kipi/notes is never written to the tree."""
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    clone = cloud_clone(tmp_path, bare, "2026-01-01T00:00:00+0000")
    push_notes_at(tmp_path, bare, "2026-09-01T00:00:00+0000",
                  {HANDOFF_PATH: "fresh\n", "q-ps/pipeline/code.py": "print('x')\n",
                   "src/app.js": "x\n", "README.md": "overwritten\n"})
    r = overlay(clone)
    assert (clone / HANDOFF_PATH).read_text() == "fresh\n", r.stdout
    assert not (clone / "q-ps/pipeline/code.py").exists()
    assert not (clone / "src/app.js").exists()
    assert (clone / "README.md").read_text() == "placeholder\n"


def test_no_kipi_notes_branch_is_a_quiet_one_liner(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    clone = cloud_clone(tmp_path, bare, "2026-01-01T00:00:00+0000")
    r = overlay(clone)
    assert r.returncode == 0
    assert (clone / HANDOFF_PATH).read_text() == "stale handoff\n"
    assert len(r.stdout.strip().splitlines()) == 1 and "no kipi/notes" in r.stdout


def test_session_start_runs_the_overlay_before_loading_the_handoff():
    src = (HERE.parents[2] / "hooks" / "session-start.py").read_text()
    assert "notes-publish.py" in src and "--overlay" in src
    main = src[src.index("def main"):]
    assert main.index("overlay_notes(") < main.index("load_handoff(")
    assert "timeout=" in src[src.index("def overlay_notes"):]
