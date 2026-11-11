"""The ``waddle`` command. Phase 1: start, stop, status, rule, config."""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import sys
import time
from pathlib import Path

from waddle_core.config import ConfigError, default_config_toml, load_config
from waddle_core.paths import WaddlePaths, resolve_paths


def _paths() -> WaddlePaths:
    return resolve_paths(os.environ, Path.home())


def running_pid(paths: WaddlePaths) -> int | None:
    """PID of the running desktop process, or None.

    Waddle holds an exclusive lock on its pid file for as long as it lives. If we can take
    the lock, nobody is running and the file is stale, whatever pid it names.
    """
    try:
        fd = os.open(paths.pid_file, os.O_RDWR | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                return int(os.read(fd, 32).decode().strip())
            except ValueError:
                return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def _pss_mb(pid: int) -> float | None:
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def _cpu_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])  # utime + stime


def cpu_percent(pid: int, seconds: float) -> float:
    start = _cpu_ticks(pid)
    time.sleep(seconds)
    return 100.0 * (_cpu_ticks(pid) - start) / os.sysconf("SC_CLK_TCK") / seconds


def cmd_start(args: argparse.Namespace) -> int:
    paths = _paths()
    if running_pid(paths) is not None:
        print("Waddle is already running.")
        return 0
    try:
        config = load_config(paths.config_file)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    from waddle_desktop import log
    from waddle_desktop.ui.app import run

    log.configure(config.logging.level, debug_unredacted=config.logging.debug_unredacted)
    return run(config, paths, demo=args.demo)


def cmd_stop(_: argparse.Namespace) -> int:
    pid = running_pid(_paths())
    if pid is None:
        print("Waddle is not running.")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)  # Waddle's own process: it holds the instance lock
    except (ProcessLookupError, PermissionError):
        print("Waddle is not running.")
        return 0
    print("Asked Waddle to stop.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    pid = running_pid(_paths())
    if pid is None:
        print("Waddle is not running.")
        return 1
    print(f"Waddle is running (pid {pid}).")
    pss = _pss_mb(pid)
    if pss is not None:
        print(f"  memory (PSS): {pss:.1f} MB   budget: < 150 MB")
    if args.measure:
        print(f"  measuring CPU for {args.measure:g} s…")
        print(f"  CPU: {cpu_percent(pid, args.measure):.2f} %   budget: ~0 % when idle")
    return 0


def cmd_rule(args: argparse.Namespace) -> int:
    from waddle_desktop.placement.kwin_rules import RuleInstaller

    paths = _paths()
    config_home = paths.config_dir.parent
    installer = RuleInstaller(config_home / "kwinrulesrc", paths.data_dir / "kwin-rules.json")
    if args.action == "status":
        status = installer.status()
        print("installed" if status.installed else "not installed")
        for item in status.drift:
            print(f"  drift: {item}")
        return 0 if status.installed else 1
    paths.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    changed = installer.install() if args.action == "install" else installer.remove()
    if changed:
        installer.reconfigure()
    print(f"Window rule {args.action}: {'done' if changed else 'nothing to do'}.")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    if args.print_default:
        print(default_config_toml())
        return 0
    print(_paths().config_file)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="waddle", description="Waddle, your on-screen tutor.")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="start Waddle in the foreground")
    start.add_argument("--demo", action="store_true", help="show sample bubbles and a mock guide")
    start.set_defaults(func=cmd_start)

    sub.add_parser("stop", help="stop the running Waddle").set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="is Waddle running, and how heavy is it")
    status.add_argument("--measure", type=float, metavar="SECONDS", help="also sample CPU usage")
    status.set_defaults(func=cmd_status)

    rule = sub.add_parser("rule", help="manage Waddle's KWin window rule")
    rule.add_argument("action", choices=["install", "remove", "status"])
    rule.set_defaults(func=cmd_rule)

    config = sub.add_parser("config", help="show the config file location")
    config.add_argument("--print-default", action="store_true", help="print a documented default")
    config.set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
