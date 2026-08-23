"""Child process control: external timeout, process-group termination, signal cleanup, reconciliation."""
from __future__ import annotations
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ChildResult:
    exit_code: int | None
    timed_out: bool
    interrupted: bool
    duration_s: float
    stdout_path: Path | None
    stderr_path: Path | None
    pid: int | None = None
    pgid: int | None = None
    idle_timed_out: bool = False
    initial_progress_timed_out: bool = False


class Interrupted(Exception):
    pass


class ChildRunner:
    """Runs one child at a time in its own process group; kills the group on timeout, SIGINT/SIGTERM, or
    when `should_stop()` returns True. Reentrant-safe for sequential use."""

    def __init__(self, on_child_start: Callable[[int, int], None] | None = None,
                 on_child_end: Callable[[int, int | None], None] | None = None):
        self._current: subprocess.Popen | None = None
        self._interrupted = False
        self._lock = threading.Lock()
        self.on_child_start = on_child_start
        self.on_child_end = on_child_end
        self._orig = {}

    # ---- signal handling
    def install_signal_handlers(self) -> None:
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                # Respect a signal the parent deliberately ignored: `nohup longrun go …` sets SIGHUP to
                # SIG_IGN so the run survives the terminal closing. Overriding that would re-arm the very
                # shutdown nohup was used to prevent — which silently killed an overnight chain once.
                if signal.getsignal(s) is signal.SIG_IGN:
                    continue
                self._orig[s] = signal.signal(s, self._handle)
            except (ValueError, OSError):
                pass

    def restore_signal_handlers(self) -> None:
        for s, h in self._orig.items():
            try:
                signal.signal(s, h)
            except (ValueError, OSError):
                pass

    def _handle(self, signum, frame) -> None:
        self._interrupted = True
        self.kill_current()

    @property
    def interrupted(self) -> bool:
        return self._interrupted

    def kill_current(self, grace: float = 3.0) -> None:
        with self._lock:
            p = self._current
        if p is None or p.poll() is not None:
            return
        _kill_group(p, grace)

    # ---- run
    def run(self, cmd: list[str], *, cwd: Path, env: dict, timeout_s: float, stdout_path: Path, stderr_path: Path,
            stdin_text: str | None = None, should_stop: Callable[[], bool] | None = None,
            on_stdout_line: Callable[[str], None] | None = None,
            idle_timeout_s: float | None = None, idle_heartbeat_s: float | None = None,
            on_idle_heartbeat: Callable[[dict], None] | None = None,
            initial_progress_timeout_s: float | None = None,
            is_initial_progress_line: Callable[[str], bool] | None = None) -> ChildResult:
        if self._interrupted:
            raise Interrupted()
        t0 = time.monotonic()
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        out_f = open(stdout_path, "ab")
        err_f = open(stderr_path, "ab")
        tracker: _DescendantTracker | None = None
        tracked_descendants: dict[int, str] = {}
        try:
            gate_r, gate_w = os.pipe()
            gate_code = ("import os,sys; fd=int(sys.argv[1]); os.read(fd,1); os.close(fd); "
                         "os.execvpe(sys.argv[2],sys.argv[2:],os.environ)")
            try:
                p = subprocess.Popen(
                    [sys.executable, "-c", gate_code, str(gate_r), *cmd], cwd=str(cwd), env=env,
                    stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE if on_stdout_line else out_f, stderr=err_f,
                    start_new_session=True, text=bool(on_stdout_line), pass_fds=(gate_r,))
            except Exception:
                os.close(gate_w)
                raise
            finally:
                os.close(gate_r)
            with self._lock:
                self._current = p
            pgid = _pgid(p)
            tracker = _DescendantTracker(p.pid)
            tracker.start()
            try:
                os.write(gate_w, b"1")
            finally:
                os.close(gate_w)
            if self.on_child_start:
                self.on_child_start(p.pid, pgid)
            if stdin_text is not None:
                try:
                    p.stdin.write(stdin_text.encode() if not on_stdout_line else stdin_text)  # type: ignore[arg-type]
                    p.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            timed_out = False
            idle_timed_out = False
            initial_progress_timed_out = False
            deadline = t0 + timeout_s
            last_output_at = [time.monotonic()]
            stream_progress = {"lines": 0, "bytes": 0}
            initial_progress_seen = [initial_progress_timeout_s is None]
            next_idle_heartbeat = [last_output_at[0] + idle_heartbeat_s if idle_heartbeat_s else None]
            if on_stdout_line:
                # stream stdout lines while watching the clock
                reader_done = threading.Event()

                def _reader():
                    try:
                        for line in p.stdout:  # type: ignore[union-attr]
                            last_output_at[0] = time.monotonic()
                            stream_progress["lines"] += 1
                            stream_progress["bytes"] += len(line.encode("utf-8", "replace") if isinstance(line, str) else line)
                            if idle_heartbeat_s:
                                next_idle_heartbeat[0] = last_output_at[0] + idle_heartbeat_s
                            if not initial_progress_seen[0] and (
                                is_initial_progress_line is None or is_initial_progress_line(line)
                            ):
                                initial_progress_seen[0] = True
                            out_f.write(line.encode("utf-8", "replace") if isinstance(line, str) else line)
                            out_f.flush()
                            try:
                                on_stdout_line(line.rstrip("\n"))
                            except Exception:
                                pass
                    finally:
                        reader_done.set()
                th = threading.Thread(target=_reader, daemon=True); th.start()
                while p.poll() is None:
                    tracked_descendants.update(process_identities(descendant_pids(p.pid)))
                    now = time.monotonic()
                    if now >= deadline:
                        timed_out = True; _kill_group(p); break
                    if (not initial_progress_seen[0] and initial_progress_timeout_s is not None
                            and now - t0 >= initial_progress_timeout_s):
                        timed_out = True; initial_progress_timed_out = True; _kill_group(p); break
                    if idle_timeout_s is not None and now - last_output_at[0] >= idle_timeout_s:
                        timed_out = True; idle_timed_out = True; _kill_group(p); break
                    if next_idle_heartbeat[0] is not None and now >= next_idle_heartbeat[0]:
                        if on_idle_heartbeat:
                            try:
                                on_idle_heartbeat({"pid": p.pid, "idle_s": round(now - last_output_at[0], 1),
                                                   "stream_lines": stream_progress["lines"],
                                                   "stream_bytes": stream_progress["bytes"]})
                            except Exception:
                                pass
                        next_idle_heartbeat[0] = now + idle_heartbeat_s  # type: ignore[operator]
                    if self._interrupted or (should_stop and should_stop()):
                        _kill_group(p); break
                    time.sleep(0.25)
                reader_done.wait(timeout=5)
            else:
                while p.poll() is None:
                    tracked_descendants.update(process_identities(descendant_pids(p.pid)))
                    if time.monotonic() >= deadline:
                        timed_out = True; _kill_group(p); break
                    if self._interrupted or (should_stop and should_stop()):
                        _kill_group(p); break
                    time.sleep(0.25)
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill_group(p, grace=0)
                p.wait(timeout=5)
            tracker.stop()
            tracked_descendants.update(tracker.identities)
            cleanup_owned_pids(tracked_descendants)
            rc = p.returncode
            if self.on_child_end:
                self.on_child_end(p.pid, rc)
            return ChildResult(exit_code=rc, timed_out=timed_out, interrupted=self._interrupted,
                               duration_s=time.monotonic() - t0, stdout_path=stdout_path, stderr_path=stderr_path,
                               pid=p.pid, pgid=pgid, idle_timed_out=idle_timed_out,
                               initial_progress_timed_out=initial_progress_timed_out)
        finally:
            if tracker is not None:
                tracker.stop()
                tracked_descendants.update(tracker.identities)
                cleanup_owned_pids(tracked_descendants)
            with self._lock:
                p = self._current
                self._current = None
            try:
                if p is not None and p.stdout is not None:
                    p.stdout.close()
            except Exception:
                pass
            out_f.close(); err_f.close()


def _pgid(p: subprocess.Popen) -> int | None:
    try:
        return os.getpgid(p.pid)
    except Exception:
        return None


def _kill_group(p: subprocess.Popen, grace: float = 3.0) -> None:
    pg = _pgid(p)
    try:
        if pg is not None:
            os.killpg(pg, signal.SIGTERM)
        else:
            p.terminate()
    except (ProcessLookupError, PermissionError):
        try:
            p.terminate()
        except Exception:
            pass
    t = time.monotonic() + grace
    while p.poll() is None and time.monotonic() < t:
        time.sleep(0.1)
    if p.poll() is None:
        try:
            if pg is not None:
                os.killpg(pg, signal.SIGKILL)
            else:
                p.kill()
        except (ProcessLookupError, PermissionError):
            try:
                p.kill()
            except Exception:
                pass


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie owns no resources and cannot execute work; treating it as alive
    # makes cleanup look unsuccessful until its unrelated parent reaps it.
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                               text=True, timeout=2).stdout.strip()
        return not state.startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return True


def process_identity(pid: int) -> str | None:
    """Return a PID birth token so cleanup cannot hit a later PID reuse."""
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        try:
            fields = proc_stat.read_text().split()
            return f"proc:{fields[21]}" if len(fields) > 21 else None
        except OSError:
            return None
    try:
        started = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                                 text=True, timeout=2).stdout.strip()
        return f"ps:{started}" if started else None
    except (OSError, subprocess.SubprocessError):
        return None


def process_identities(pids: set[int]) -> dict[int, str]:
    return {pid: token for pid in pids if (token := process_identity(pid)) is not None}


def descendant_pids(root_pid: int) -> set[int]:
    """Snapshot descendants while they are still attached to the owned child.

    This complements process-group and environment-marker cleanup: a child may
    deliberately clear its environment before exec, but it cannot stop being a
    descendant retroactively. Sampling during the foreground session preserves
    that ownership fact after a later setsid/reparent.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    children: dict[int, set[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, set()).add(pid)
    found: set[int] = set()
    frontier = list(children.get(root_pid, ()))
    while frontier:
        pid = frontier.pop()
        if pid in found:
            continue
        found.add(pid)
        frontier.extend(children.get(pid, ()))
    return found


class _DescendantTracker:
    """Track descendants from before the owned command is allowed to exec.

    Darwin kqueue NOTE_TRACK records ordinary forked children in the kernel;
    other POSIX systems retain gated fast polling. This supplements the normal
    process-group/marker boundaries but is not a hostile same-user sandbox.
    """
    def __init__(self, root_pid: int):
        self.root_pid = root_pid
        self.identities: dict[int, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._kqueue = None

    def start(self) -> None:
        if hasattr(select, "kqueue") and hasattr(select, "KQ_NOTE_TRACK"):
            try:
                self._kqueue = select.kqueue()
                event = select.kevent(
                    self.root_pid, filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                    fflags=select.KQ_NOTE_FORK | select.KQ_NOTE_TRACK | select.KQ_NOTE_EXIT)
                self._kqueue.control([event], 0, 0)
            except (OSError, AttributeError):
                if self._kqueue is not None:
                    self._kqueue.close()
                self._kqueue = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        next_snapshot = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_snapshot:
                self.identities.update(process_identities(descendant_pids(self.root_pid)))
                next_snapshot = now + 0.25
            if self._kqueue is not None:
                try:
                    for event in self._kqueue.control(None, 64, 0.05):
                        if event.ident != self.root_pid:
                            pid = int(event.ident)
                            token = process_identity(pid)
                            if token is not None:
                                self.identities[pid] = token
                except OSError:
                    self._kqueue.close()
                    self._kqueue = None
            else:
                self._stop.wait(0.05)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
        if self._kqueue is not None:
            self._kqueue.close()
            self._kqueue = None


def cleanup_owned_pids(pids: dict[int, str], grace: float = 1.0) -> list[int]:
    """Terminate observed descendants only while their birth identity matches."""
    owned = sorted(pid for pid, token in pids.items()
                   if pid not in {os.getpid(), os.getppid()} and process_identity(pid) == token)
    for pid in owned:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(pid_alive(pid) for pid in owned):
        time.sleep(0.05)
    for pid in owned:
        if pid_alive(pid) and process_identity(pid) == pids.get(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return owned


def processes_with_env_marker(marker: str) -> list[int]:
    """Return same-user processes that inherited one exact session marker.

    Process-group cleanup cannot see children that called setsid/daemonized.
    The marker follows those children through exec and gives the controller a
    second, ownership-scoped cleanup boundary.
    """
    if not marker:
        return []
    needle = f"LONGRUN_SESSION_MARKER={marker}"
    found: set[int] = set()
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "environ").read_bytes().split(b"\0")
                if needle.encode() in raw:
                    found.add(int(entry.name))
            except (OSError, ValueError):
                continue
    else:
        try:
            out = subprocess.run(["ps", "eww", "-axo", "pid=,command="], capture_output=True,
                                 text=True, timeout=10).stdout
            for line in out.splitlines():
                if needle not in line:
                    continue
                m = re.match(r"\s*(\d+)\s", line)
                if m:
                    found.add(int(m.group(1)))
        except (OSError, subprocess.SubprocessError):
            pass
    found.discard(os.getpid())
    found.discard(os.getppid())
    return sorted(found)


def cleanup_processes_with_env_marker(marker: str, grace: float = 1.0) -> list[int]:
    """Terminate every escaped descendant owned by one finished session."""
    pids = processes_with_env_marker(marker)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(pid_alive(pid) for pid in pids):
        time.sleep(0.05)
    for pid in pids:
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return pids


def reconcile_children(children: list[dict]) -> list[dict]:
    """On controller startup: kill any child process groups recorded as still-running by a previous
    controller instance of this run, and mark them ended."""
    out = []
    for c in children:
        if c.get("ended_at") is None and c.get("pgid"):
            if pid_alive(c["pid"]):
                try:
                    os.killpg(int(c["pgid"]), signal.SIGTERM)
                    time.sleep(1.0)
                    if pid_alive(c["pid"]):
                        os.killpg(int(c["pgid"]), signal.SIGKILL)
                except Exception:
                    pass
            c = dict(c); c["ended_at"] = "reconciled"; c["exit"] = c.get("exit", "killed_on_reconcile")
        out.append(c)
    return out
