"""Restart a local Docker replay only after a clean disk-floor stop."""

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from common import AuditError, load_config, load_games
from monitor import stop_nodes


def container_status(name):
    result = subprocess.run(["docker", "inspect", "--format", "{{.State.Status}}", name],
                            capture_output=True, text=True, timeout=20, check=True)
    return result.stdout.strip()


def nodes_stopped(config):
    return all(container_status(name) in ("exited", "created")
               for name in config["docker_containers"])


def wait_for_space(config, deadline):
    required = float(config.get("restart_free_gib", 6)) * 2**30
    window_floor = float(config.get("restart_window_free_gib", 5.5)) * 2**30
    if required < window_floor or window_floor <= 0:
        raise AuditError("invalid restart space thresholds")
    samples = deque(maxlen=18)
    while time.monotonic() < deadline:
        free = shutil.disk_usage(config["data_dir"]).free
        samples.append(free)
        if len(samples) == 18 and free >= required and min(samples) >= window_floor:
            if nodes_stopped(config):
                return
            raise AuditError("recovery gate passed while a replay node was running")
        time.sleep(10)
    raise AuditError("disk recovery deadline reached")


def read_monitor_log(path):
    verified = []
    terminal = None
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row.get("event") == "game_verified" and row.get("matched") is True:
            verified.append(row["game"])
        elif row.get("event") == "stopped":
            terminal = row
    return max(verified, default=None), terminal


def run(config, start_index, through_index, max_restarts, max_hours):
    if not config.get("docker_containers"):
        raise AuditError("supervisor requires docker_containers")
    games = load_games(config)
    if not 0 <= start_index <= through_index < len(games):
        raise AuditError("start/through index is outside the retained schedule")
    if not nodes_stopped(config):
        raise AuditError("replay nodes must be stopped before supervisor starts")
    deadline = time.monotonic() + max_hours * 3600
    restart = 0
    current_start = start_index
    while time.monotonic() < deadline:
        wait_for_space(config, deadline)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log = config["data_dir"] / f"monitor-{stamp}-from-{current_start}.jsonl"
        try:
            for name in config["docker_containers"]:
                subprocess.run(["docker", "start", name], check=True, timeout=30,
                               capture_output=True, text=True)
            command = [sys.executable, str(Path(__file__).with_name("monitor.py")),
                       "--config", str(config["config_path"]),
                       "--start-index", str(current_start),
                       "--through-index", str(through_index),
                       "--log", str(log), "--max-hours", str(max_hours)]
            child = subprocess.run(command, check=False)
        finally:
            failures = stop_nodes(config)
        if failures:
            raise AuditError(f"could not stop all replay nodes: {failures}")
        if not log.exists():
            raise AuditError("monitor produced no log; refusing restart")
        last_good, terminal = read_monitor_log(log)
        if terminal is None:
            raise AuditError("monitor produced no terminal event; refusing restart")
        reason = terminal["reason"]
        if reason == "target_reached" and last_good == through_index and child.returncode == 0:
            print(json.dumps({"event": "complete", "verified_through": through_index,
                              "log": str(log)}))
            return 0
        if reason != "disk_floor" or child.returncode != 3:
            raise AuditError(f"refusing restart after {reason}; log={log}")
        expected_next = (last_good + 1) if last_good is not None else current_start
        if terminal.get("next_game") != expected_next:
            raise AuditError("monitor checkpoint sequence is inconsistent")
        if restart >= max_restarts:
            raise AuditError("disk restart limit reached")
        restart += 1
        current_start = last_good if last_good is not None else current_start
        print(json.dumps({"event": "disk_stop", "restart": restart,
                          "last_verified": last_good, "log": str(log)}), flush=True)
    raise AuditError("supervisor runtime limit reached")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--start-index", type=int, required=True)
    parser.add_argument("--through-index", type=int, required=True)
    parser.add_argument("--max-restarts", type=int, default=3)
    parser.add_argument("--max-hours", type=float, default=24)
    args = parser.parse_args()
    if args.max_restarts < 0 or args.max_hours <= 0:
        parser.error("invalid restart or runtime limit")
    try:
        sys.exit(run(load_config(args.config), args.start_index,
                     args.through_index, args.max_restarts, args.max_hours))
    except (AuditError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"supervisor stopped: {exc}\n")
