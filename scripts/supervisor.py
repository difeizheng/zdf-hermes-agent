#!/usr/bin/env python3
"""Supervisor: manages coordinator + 4 agent daemons as a single process group.

Usage:
    python scripts/supervisor.py start [--background]
    python scripts/supervisor.py stop
    python scripts/supervisor.py status
    python scripts/supervisor.py logs [--follow]
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [supervisor] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class ProcessDef:
    name: str
    module: str | None = None
    script: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    pid: int | None = None
    process: subprocess.Popen | None = None


AGENT_TYPES = ["design", "dev", "security", "qa", "validate", "deploy"]


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))


def _pid_dir() -> Path:
    d = Path(_hermes_home()) / "pids"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_dir() -> Path:
    d = Path(_hermes_home()) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_pid(name: str, pid: int) -> None:
    (_pid_dir() / f"{name}.pid").write_text(str(pid))


def _read_pid(name: str) -> int | None:
    p = _pid_dir() / f"{name}.pid"
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except ValueError:
        return None


def _remove_pid(name: str) -> None:
    p = _pid_dir() / f"{name}.pid"
    p.unlink(missing_ok=True)


def _find_pid_by_port(port: int) -> int | None:
    """Find PID of process listening on a TCP port. Returns None if not found."""
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                for conn in proc.net_connections(kind="inet"):
                    if conn.laddr.port == port and conn.status == "LISTEN":
                        return proc.pid
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
    except Exception:
        pass
    return None


def _is_alive(pid: int, name: str = "") -> bool:
    # Coordinator: always verify via health check when PID file is dead
    # because uvicorn runs as a child of the launcher which exits immediately.
    if name == "coordinator":
        try:
            import urllib.request
            req = urllib.request.urlopen("http://localhost:9100/health", timeout=2)
            return req.status == 200
        except Exception:
            return False
    # Agents: use PID-based check
    if sys.platform == "win32":
        try:
            import psutil
            return psutil.pid_exists(pid)
        except ImportError:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True,
            )
            return str(pid) in result.stdout
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _build_processes(port: int, coordinator_url: str, agent_ids: dict[str, str] | None = None) -> list[ProcessDef]:
    if agent_ids is None:
        agent_ids = {t: f"{t}-1" for t in AGENT_TYPES}

    coordinator = ProcessDef(
        name="coordinator",
        module="uvicorn",
        args=["coordinator.server:app", "--host", "0.0.0.0", "--port", str(port), "--log-level", "info"],
        env={"COORDINATOR_PORT": str(port)},
    )

    # Optional progress watcher — pushes task status events to DingTalk
    watcher = ProcessDef(
        name="progress-watcher",
        script=str(ROOT / "scripts" / "progress_watcher.py"),
        args=["--coordinator-url", coordinator_url],
    )

    agents = []
    for agent_type in AGENT_TYPES:
        agents.append(ProcessDef(
            name=f"agent-{agent_type}",
            script=str(ROOT / "scripts" / f"run_{agent_type}_agent.py"),
            args=[
                "--agent-id", agent_ids.get(agent_type, f"{agent_type}-1"),
                "--coordinator-url", coordinator_url,
            ],
        ))

    return [coordinator, watcher] + agents


def start(background: bool = False, port: int = 9100, coordinator_url: str = "http://localhost:9100") -> None:
    """Start coordinator + all agents."""
    processes = _build_processes(port, coordinator_url)

    # Check if already running
    running = []
    for p in processes:
        pid = _read_pid(p.name)
        # Coordinator: use health check for liveness (PID file may hold child PID)
        if pid and _is_alive(pid, name=p.name):
            running.append(p.name)

    if running:
        logger.warning("Already running: %s. Stop first.", ", ".join(running))
        return

    if not background:
        logger.info("Starting %d processes in foreground...", len(processes))
        _start_foreground(processes)
    else:
        logger.info("Starting %d processes in background...", len(processes))
        _start_background(processes)


def _start_foreground(processes: list[ProcessDef]) -> None:
    """Start coordinator, wait, then start agents in foreground with process group."""
    coordinator = processes[0]
    agents = processes[1:]

    log_dir = _log_dir()

    # Start coordinator
    coord_proc = _launch(coordinator, log_dir / "coordinator.log", create_session=True)
    _write_pid("coordinator", coord_proc.pid)
    logger.info("Coordinator launcher started (PID %d)", coord_proc.pid)

    # Wait for coordinator health
    if not _wait_for_coordinator("http://localhost:9100", timeout=30):
        logger.error("Coordinator failed to start. Aborting.")
        _kill_process(coord_proc)
        _remove_pid("coordinator")
        return

    # Update PID file with the actual uvicorn child process PID (found by port)
    actual_pid = _find_pid_by_port(9100)
    if actual_pid:
        _write_pid("coordinator", actual_pid)
        logger.info("Coordinator running as PID %d (found by port)", actual_pid)

    # Start agents
    agent_procs = []
    for agent in agents:
        log_file = log_dir / f"{agent.name}.log"
        proc = _launch(agent, log_file)
        agent.procname = agent.name  # type: ignore
        _write_pid(agent.name, proc.pid)
        agent_procs.append(proc)
        logger.info("%s started (PID %d)", agent.name, proc.pid)
        time.sleep(1)  # stagger

    # Set up signal handlers
    def _signal_handler(sig: int, _frame: Any) -> None:
        sig_name = signal.Signals(sig).name
        logger.info("Received %s, shutting down...", sig_name)
        _kill_process(coord_proc)
        _remove_pid("coordinator")
        for proc in agent_procs:
            _kill_process(proc)
            _remove_pid(proc.name)  # type: ignore
        sys.exit(0)

    if sys.platform != "win32":
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

    try:
        coord_proc.wait()
    except KeyboardInterrupt:
        _signal_handler(signal.SIGINT, None)


def _start_background(processes: list[ProcessDef]) -> None:
    log_dir = _log_dir()

    # Start coordinator
    coord_proc = _launch(processes[0], log_dir / "coordinator.log", detach=True)
    # Write launcher PID as placeholder — it exits immediately (uvicorn forks a child process)
    _write_pid("coordinator", coord_proc.pid)
    logger.info("Coordinator launcher started (PID %d)", coord_proc.pid)

    # Wait for health — uvicorn child process will be running by then
    if not _wait_for_coordinator("http://localhost:9100", timeout=30):
        logger.error("Coordinator failed to start. Check %s", log_dir / "coordinator.log")
        _remove_pid("coordinator")
        return

    # Update PID file with the actual uvicorn child process PID (found by port)
    actual_pid = _find_pid_by_port(9100)
    if actual_pid:
        _write_pid("coordinator", actual_pid)
        logger.info("Coordinator running as PID %d (found by port)", actual_pid)
    else:
        logger.warning("Could not find coordinator PID by port, health check passed")

    # Start agents
    for agent in processes[1:]:
        log_file = log_dir / f"{agent.name}.log"
        proc = _launch(agent, log_file, detach=True)
        _write_pid(agent.name, proc.pid)
        logger.info("%s started (PID %d)", agent.name, proc.pid)
        time.sleep(1)

    logger.info("All processes started. Use 'stop' to shut down.")


def _launch(proc: ProcessDef, log_file: Path, detach: bool = False, create_session: bool = False) -> subprocess.Popen:
    """Launch a single process."""
    env = os.environ.copy()
    env.update(proc.env)
    env.setdefault("NO_PROXY", "127.0.0.1,localhost")

    # Use project venv python to ensure dependencies (uvicorn, httpx) are available
    venv_python = Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe"
    python_exe = venv_python if venv_python.exists() else sys.executable

    if proc.module:
        cmd = [python_exe, "-m", proc.module, *proc.args]
    else:
        cmd = [python_exe, proc.script, *proc.args]

    open_kwargs: dict[str, Any] = {"stdout": open(log_file, "a"), "stderr": subprocess.STDOUT, "env": env}

    if sys.platform != "win32":
        if create_session:
            open_kwargs["start_new_session"] = True
        elif detach:
            open_kwargs["stdin"] = subprocess.DEVNULL

    return subprocess.Popen(cmd, **open_kwargs)


def _kill_by_pid(pid: int) -> None:
    """Kill a process by PID (Windows + Unix)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _kill_process(proc: subprocess.Popen) -> None:
    """Graceful shutdown with force fallback."""
    if proc.poll() is not None:
        return

    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T"], capture_output=True)
    else:
        import signal as sig
        try:
            os.killpg(os.getpgid(proc.pid), sig.SIGTERM)
        except OSError:
            proc.terminate()

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/F", "/T"], capture_output=True)
        else:
            proc.kill()


def _wait_for_coordinator(url: str, timeout: int = 30) -> bool:
    """Poll coordinator health endpoint."""
    import urllib.request
    health_url = f"{url}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(health_url, timeout=2)
            if resp.status == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def stop() -> None:
    """Stop all managed processes."""
    names = ["coordinator"] + [f"agent-{t}" for t in AGENT_TYPES]
    stopped = []

    for name in names:
        pid = _read_pid(name)
        # Coordinator: find actual PID via port (PID file may hold wrong child PID)
        if name == "coordinator":
            actual_pid = _find_pid_by_port(9100)
            if actual_pid and _is_alive(actual_pid, name="coordinator"):
                logger.info("Stopping coordinator (PID %d)...", actual_pid)
                _kill_by_pid(actual_pid)
                stopped.append(name)
            else:
                logger.debug("Coordinator not running (port check)")
        elif pid and _is_alive(pid):
            logger.info("Stopping %s (PID %d)...", name, pid)
            _kill_by_pid(pid)
            stopped.append(name)
        else:
            logger.debug("%s not running", name)
        _remove_pid(name)

    # Wait for graceful shutdown
    time.sleep(3)

    # Force kill survivors
    for name in stopped:
        if name == "coordinator":
            pid = _find_pid_by_port(9100)
        else:
            pid = _read_pid(name)
        if pid:
            if _is_alive(pid, name=name):
                logger.warning("Force killing %s (PID %d)", name, pid)
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
                else:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
        _remove_pid(name)

    if stopped:
        logger.info("Stopped: %s", ", ".join(stopped))
    else:
        logger.info("No processes were running.")


def status() -> None:
    """Show status of all managed processes."""
    names = ["coordinator"] + [f"agent-{t}" for t in AGENT_TYPES]
    any_running = False

    for name in names:
        pid = _read_pid(name)
        # Coordinator: use health check to determine liveness (PID file may hold child process PID)
        alive = pid is not None and _is_alive(pid, name=name)
        status_str = f"RUNNING (PID {pid})" if alive else "STOPPED"
        if alive:
            any_running = True
        logger.info("%-20s %s", name, status_str)

    if any_running:
        # Health check
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://localhost:9100/health", timeout=2)
            logger.info("Coordinator health: OK (%d)", resp.status)
        except Exception:
            logger.info("Coordinator health: FAIL")


def logs(follow: bool = False) -> None:
    """Tail coordinator log."""
    log_file = _log_dir() / "coordinator.log"
    if not log_file.exists():
        logger.info("No log file found at %s", log_file)
        return

    if follow:
        if sys.platform == "win32":
            subprocess.run(["powershell", "-Command", f"Get-Content -Wait '{log_file}'"])
        else:
            subprocess.run(["tail", "-f", str(log_file)])
    else:
        print(log_file.read_text()[-5000:])  # last 5000 chars


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Orchestrator Supervisor")
    sub = parser.add_subparsers(dest="command")

    start_p = sub.add_parser("start", help="Start coordinator + agents")
    start_p.add_argument("--background", "-b", action="store_true")
    start_p.add_argument("--port", type=int, default=9100)
    start_p.add_argument("--coordinator-url", default="http://localhost:9100")

    sub.add_parser("stop", help="Stop all processes")
    sub.add_parser("status", help="Show process status")

    logs_p = sub.add_parser("logs", help="View coordinator log")
    logs_p.add_argument("--follow", "-f", action="store_true")

    args = parser.parse_args()

    if args.command == "start":
        start(
            background=args.background,
            port=args.port,
            coordinator_url=args.coordinator_url,
        )
    elif args.command == "stop":
        stop()
    elif args.command == "status":
        status()
    elif args.command == "logs":
        logs(follow=args.follow)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
