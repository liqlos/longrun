"""Git helpers: revision/content hashing, worktree isolation, diff summaries. Never touches user branches."""
from __future__ import annotations
import difflib
import hashlib
import fnmatch
import json
import shutil
import subprocess
import threading
from pathlib import Path


def _git(cwd: Path, *args: str, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr if p.returncode else "")


def _git_limited(cwd: Path, args: list[str], max_bytes: int, timeout: int = 120) -> tuple[int, str, bool]:
    """Read at most max_bytes from a potentially huge git command without buffering it in RAM."""
    p = subprocess.Popen(["git", *args], cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    data = bytearray()

    def read_bounded() -> None:
        while len(data) <= max_bytes and p.stdout:
            chunk = p.stdout.read(min(64 * 1024, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)

    reader = threading.Thread(target=read_bounded, daemon=True)
    reader.start()
    try:
        reader.join(timeout)
        if reader.is_alive():
            p.kill(); p.wait(); reader.join(1)
            return 124, bytes(data[:max_bytes]).decode("utf-8", "replace"), len(data) > max_bytes
        truncated = len(data) > max_bytes
        if truncated:
            p.terminate()
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait()
            return 124, bytes(data[:max_bytes]).decode("utf-8", "replace"), truncated
        return (0 if truncated else p.returncode), bytes(data[:max_bytes]).decode("utf-8", "replace"), truncated
    finally:
        if p.stdout:
            p.stdout.close()


def is_git_repo(p: Path) -> bool:
    rc, _ = _git(p, "rev-parse", "--is-inside-work-tree")
    return rc == 0


def head(p: Path) -> str | None:
    rc, out = _git(p, "rev-parse", "HEAD")
    return out.strip() if rc == 0 else None


def is_dirty(p: Path) -> bool:
    # `git status --porcelain` refreshes and walks the full tracked Unity tree;
    # on the Skyline checkout that exceeded the 120 s controller timeout before
    # a run could even be created.  diff-index answers the exact question this
    # helper asks (tracked worktree differs from HEAD) without the status walk.
    rc, _ = _git(p, "diff-index", "--quiet", "HEAD", "--")
    if rc in (0, 1):
        return rc == 1
    # Preserve compatibility for unusual repositories where diff-index cannot
    # resolve HEAD (for example an unborn branch).
    rc, out = _git(p, "status", "--porcelain", "--untracked-files=no")
    return rc == 0 and bool(out.strip())


def content_revision(p: Path) -> str:
    """HEAD sha plus a content hash of the working-tree diff (tracked files). Stable across identical states.
    Non-git directories get a walk hash of file mtimes+sizes (best effort)."""
    h = head(p)
    if h is None:
        return "nogit-" + _tree_hash(p)
    # Do not ask `git diff` to materialize LFS-cleaned content here. A dirty
    # Unity checkout with several changed FBXs spent minutes inside git-lfs and
    # timed out before planning. diff-index identifies the tracked paths in
    # milliseconds; hashing their actual bytes preserves content identity
    # without invoking clean filters.
    rc, changed_out = _git(p, "diff-index", "--name-only", "-z", "HEAD", "--")
    changed = [x for x in changed_out.split("\0") if x] if rc == 0 else []
    hh = hashlib.sha256()
    for rel in sorted(changed):
        f = p / rel
        hh.update(rel.encode("utf-8", "surrogateescape")); hh.update(b"\0")
        try:
            if f.is_file():
                _hash_worktree_file(hh, f)
            else:
                hh.update(b"<deleted>")
        except OSError:
            hh.update(b"<unreadable>")
    rc2, others = _git(p, "ls-files", "--others", "--exclude-standard", "-z")
    untracked = [x for x in others.split("\0") if x] if rc2 == 0 else []
    for rel in sorted(untracked):
        f = p / rel
        try:
            hh.update(rel.encode()); _hash_worktree_file(hh, f)
        except OSError:
            continue
    dirty = bool(changed) or bool(untracked)
    d = hh.hexdigest()[:12] if dirty else "clean"
    return f"{h}+{d}"


def _hash_worktree_file(hh, path: Path, *, block_size: int = 1024 * 1024) -> None:
    """Bind a worktree file byte-for-byte with bounded process memory."""
    size = path.stat().st_size
    hh.update(str(size).encode()); hh.update(b"\0")
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(block_size), b""):
            hh.update(block)


def _tree_hash(p: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(x for x in p.rglob("*") if x.is_file() and ".git" not in x.parts and ".longrun" not in x.parts):
        try:
            st = f.stat()
            h.update(f"{f.relative_to(p)}:{st.st_size}:{int(st.st_mtime)}".encode())
        except OSError:
            continue
    return h.hexdigest()[:16]


def diff_text(p: Path, base: str | None, max_bytes: int = 200_000, exclude_globs: list[str] | None = None) -> str:
    """Return a bounded diff, withholding configured and unusually large worktree files.

    The withheld paths are still named. This avoids materializing multi-gigabyte Unity/LFS diffs merely to
    tell an evaluator that those generated files changed.
    """
    if base is None:
        return ""
    rc, changed_out = _git(p, "diff-index", "--name-only", "-z", base, "--")
    changed = [x for x in changed_out.split("\0") if x] if rc == 0 else []
    rc, untracked_out = _git(p, "ls-files", "--others", "--exclude-standard", "-z")
    untracked = [x for x in untracked_out.split("\0") if x] if rc == 0 else []
    configured = list(exclude_globs or [])
    untracked = [rel for rel in untracked if not any(fnmatch.fnmatch(rel, pat) for pat in configured)]
    excluded = {rel for rel in changed if any(fnmatch.fnmatch(rel, pat) for pat in configured)}
    large = set()
    for rel in changed:
        try:
            if (p / rel).is_file() and (p / rel).stat().st_size > 16 * 1024 * 1024:
                large.add(rel)
        except OSError:
            pass
    excluded.update(large)
    pathspec = (["--"] + [f":(exclude,glob){g}" for g in configured]
                + [f":(exclude,literal){rel}" for rel in sorted(large)]) if configured or large else []
    rc, out = _git(p, "diff", base, "--stat", *pathspec)
    stat = out if rc == 0 else ""
    if max_bytes > 0:
        rc, body, truncated = _git_limited(p, ["diff", base, *pathspec], max_bytes)
        body = body if rc == 0 else ""
    else:
        body, truncated = "", False
    note = ""
    if excluded:
        note = (f"\n... [configured or large paths withheld from this diff: {', '.join(sorted(excluded))}. "
                f"Read those files in the workspace if a criterion turns on them.]")
    if truncated:
        body += f"\n... [diff truncated after {max_bytes} bytes]"
    if untracked:
        rows = []
        for rel in sorted(untracked):
            try:
                f = p / rel
                if f.is_file():
                    size = f.stat().st_size
                    if size <= 16 * 1024 * 1024:
                        hh = hashlib.sha256(); _hash_worktree_file(hh, f)
                        rows.append(f"?? {rel} ({size} bytes, content-hash={hh.hexdigest()})")
                    else:
                        rows.append(f"?? {rel} ({size} bytes, content withheld; inspect workspace path)")
                else:
                    rows.append(f"?? {rel}")
            except OSError:
                rows.append(f"?? {rel} (unreadable)")
        note += "\n[untracked workspace files; evaluator may inspect these paths]\n" + "\n".join(rows)
    return (stat + "\n" + body + note).strip()


def _plain_file_hash(path: Path) -> str:
    hh = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hh.update(chunk)
    return hh.hexdigest()


def snapshot_dirty_baseline(p: Path, base: str | None, dest: Path) -> dict[str, dict]:
    """Snapshot owner-dirty paths so later evaluation credits only run delta."""
    dest.mkdir(parents=True, exist_ok=True)
    files_dir = dest / "files"
    manifest: dict[str, dict] = {}
    for rel in sorted(set(changed_files(p, base))):
        src = p / rel
        entry = {"exists": src.is_file()}
        if src.is_file():
            try:
                size = src.stat().st_size
                entry.update({"size": size, "sha256": _plain_file_hash(src)})
                if size <= 2 * 1024 * 1024:
                    copy = files_dir / rel
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, copy)
                    entry["copy"] = str(copy.relative_to(dest))
            except OSError:
                entry["unreadable"] = True
        manifest[rel] = entry
    (dest / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
    return manifest


def diff_text_from_dirty_baseline(p: Path, base: str | None, snapshot_dir: Path,
                                  max_bytes: int = 200_000,
                                  exclude_globs: list[str] | None = None) -> str:
    """Diff current workspace against the exact dirty in-place freeze state."""
    try:
        manifest = json.loads((snapshot_dir / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        return diff_text(p, base, max_bytes=max_bytes, exclude_globs=exclude_globs)
    baseline_paths = list(manifest)
    out = diff_text(p, base, max_bytes=max_bytes,
                    exclude_globs=list(exclude_globs or []) + baseline_paths)
    deltas: list[str] = []
    for rel, before in sorted(manifest.items()):
        current = p / rel
        current_exists = current.is_file()
        try:
            current_hash = _plain_file_hash(current) if current_exists else None
        except OSError:
            current_hash = "<unreadable>"
        if current_exists == bool(before.get("exists")) and current_hash == before.get("sha256"):
            continue
        header = (f"\n[run delta for owner-dirty baseline path {rel}: "
                  f"{before.get('sha256') or '<absent>'} -> {current_hash or '<absent>'}]")
        copy_rel = before.get("copy")
        if copy_rel and current_exists and current.stat().st_size <= 2 * 1024 * 1024:
            try:
                old_raw = (snapshot_dir / copy_rel).read_bytes()
                new_raw = current.read_bytes()
                if b"\0" not in old_raw and b"\0" not in new_raw:
                    delta = "".join(difflib.unified_diff(
                        old_raw.decode("utf-8", "replace").splitlines(True),
                        new_raw.decode("utf-8", "replace").splitlines(True),
                        fromfile=f"baseline/{rel}", tofile=f"workspace/{rel}"))
                    deltas.append(header + "\n" + delta[:max_bytes])
                    continue
            except OSError:
                pass
        deltas.append(header)
    return (out + "\n" + "\n".join(deltas)).strip()


def changed_files(p: Path, base: str | None) -> list[str]:
    if base is None:
        return []
    rc, out = _git(p, "diff", "--name-only", base)
    files = [x for x in out.splitlines() if x.strip()] if rc == 0 else []
    rc, out = _git(p, "ls-files", "--others", "--exclude-standard")
    if rc == 0:
        files += [x for x in out.splitlines() if x.strip()]
    return files


def add_worktree(repo: Path, dest: Path, branch: str, start: str) -> tuple[bool, str]:
    rc, out = _git(repo, "worktree", "add", "-b", branch, str(dest), start)
    return rc == 0, out


def remove_worktree(repo: Path, dest: Path) -> tuple[bool, str]:
    rc, out = _git(repo, "worktree", "remove", "--force", str(dest))
    return rc == 0, out


def save_patch(p: Path, base: str, dest: Path) -> bool:
    """Stream a recovery patch to disk; never hold a generated/binary diff in process memory."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dest.open("wb") as out:
            proc = subprocess.run(["git", "diff", base, "--binary"], cwd=str(p), stdout=out,
                                  stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        dest.unlink(missing_ok=True)
        return False
    if proc.returncode != 0:
        dest.unlink(missing_ok=True)
        return False
    return True


def hard_reset(p: Path, rev: str) -> bool:
    """Reset tracked content while preserving untracked builder artifacts.

    Recovery patches contain tracked diffs only. Cleaning untracked files here
    therefore made restart-manager DISCARD irreversibly lossy.
    """
    rc, _ = _git(p, "reset", "--hard", rev)
    return rc == 0


def fast_forward_into(repo: Path, branch: str) -> tuple[bool, str]:
    """Bring a finished run's worktree branch into the project's checked-out branch, fast-forward only.
    Refuses when the project has uncommitted changes or the branch does not fast-forward."""
    # Ask only for tracked changed paths. `git status` walks the entire Unity
    # checkout and refreshes filters; on large LFS scenes that is both slow and
    # memory-hungry. Untracked files intentionally do not block fast-forward.
    rc, out = _git(repo, "diff-index", "--name-only", "HEAD", "--")
    if rc != 0:
        return False, out
    # Untracked files never block a fast-forward, and neither does the harness's own bookkeeping: `.longrun/`
    # holds the run-history ledger this very chain appends to when the previous outcome finished, so a strict
    # check made every second landing refuse for a file the harness had dirtied itself (2026-08-18: both
    # outcomes of a chain refused, then carried into main by hand). Git still refuses a real conflict below.
    dirty = [line for line in out.splitlines() if line and not line.startswith(".longrun/")]
    if dirty:
        return False, "project working tree has uncommitted changes; not merging: " + ", ".join(dirty[:5])
    rc, out = _git(repo, "merge", "--ff-only", branch)
    return rc == 0, out.strip()[-300:]


def unmerged_run_branches(repo: Path) -> list[tuple[str, int]]:
    """Run branches holding commits the checked-out branch does not have.
    Work stranded here means the next outcome would start from a stale base."""
    rc, out = _git(repo, "branch", "--list", "longrun/*", "--format=%(refname:short)")
    if rc != 0:
        return []
    stranded = []
    for br in [l.strip() for l in out.splitlines() if l.strip()]:
        rc, cnt = _git(repo, "rev-list", "--count", f"HEAD..{br}")
        if rc == 0 and cnt.strip().isdigit() and int(cnt.strip()) > 0:
            stranded.append((br, int(cnt.strip())))
    return stranded
