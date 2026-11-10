"""Verify a finite set of L1 games while guarding a local Docker replay."""

import argparse
import json
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from common import AuditError, Mismatch, RPC, load_config, load_games
from verify_checkpoint import verify_index


def quantity(value):
    return int(value, 16) if isinstance(value, str) else int(value)


def stop_nodes(config):
    failures = []
    for name in reversed(config["docker_containers"]):
        try:
            state = subprocess.run(["docker", "inspect", "--format",
                                    "{{.State.Status}}", name],
                                   capture_output=True, text=True, timeout=20,
                                   check=True).stdout.strip()
            if state in ("exited", "created"):
                continue
            result = subprocess.run(["docker", "stop", "-t", "10", name],
                                    capture_output=True, text=True, timeout=30,
                                    check=False)
            if result.returncode:
                failures.append(f"{name}: {result.stderr.strip()[:120]}")
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"{name}: {str(exc)[:120]}")
    return failures


def run(config, start_index, through_index, log_path, max_hours):
    if not config.get("docker_containers"):
        raise AuditError("monitor requires docker_containers for the disk-stop guard")
    games = load_games(config)
    if not 0 <= start_index <= through_index < len(games):
        raise AuditError("start/through index is outside the retained schedule")
    floor = float(config.get("disk_floor_gib", 5)) * 2**30
    if floor <= 0:
        raise AuditError("disk_floor_gib must be positive")
    local = RPC(config["local_l2_rpc"])
    op_node = RPC(config["op_node_rpc"])
    fast_floor = threading.Event()
    stop_lock = threading.Lock()
    deadline = time.monotonic() + max_hours * 3600
    next_index = start_index
    poll_failures = 0
    check_failures = 0
    reason = "interrupted"
    head = safe_head = None
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(event, **fields):
        row = {"time_utc": datetime.now(timezone.utc).isoformat(),
               "event": event, **fields}
        line = json.dumps(row, sort_keys=True)
        with log_path.open("a") as output:
            output.write(line + "\n")
        print(line, flush=True)

    def guarded_stop():
        with stop_lock:
            for error in stop_nodes(config):
                emit("stop_error", error=error)

    def watch_disk():
        while not fast_floor.is_set():
            try:
                low = shutil.disk_usage(config["data_dir"]).free < floor
            except OSError as exc:
                emit("disk_guard_error", error=str(exc)[:180])
                low = True
            if low:
                fast_floor.set()
                emit("fast_disk_floor")
                guarded_stop()
                return
            time.sleep(1)

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signal.signal(getattr(signal, name), interrupted)
    threading.Thread(target=watch_disk, daemon=True).start()

    try:
        while True:
            if fast_floor.is_set() or shutil.disk_usage(config["data_dir"]).free < floor:
                reason = "disk_floor"
                break
            try:
                head = quantity(local.call("eth_blockNumber", [], timeout=8))
                status = op_node.call("optimism_syncStatus", [], timeout=8)
                safe = status.get("safe_l2")
                if not isinstance(safe, dict) or safe.get("number") is None:
                    raise AuditError("op-node did not provide safe_l2.number")
                safe_head = quantity(safe["number"])
                origin = status.get("current_l1") or {}
                poll_failures = 0
                emit("progress", head=head, safe_head=safe_head,
                     current_l1=origin.get("number"), next_game=next_index,
                     free_gib=round(shutil.disk_usage(config["data_dir"]).free / 2**30, 2))
            except (AuditError, ValueError, TypeError, KeyError) as exc:
                head = safe_head = None
                poll_failures += 1
                emit("poll_error", failures=poll_failures, error=str(exc)[:220])
                if poll_failures >= 5:
                    reason = "repeated_poll_failure"
                    break
            if fast_floor.is_set():
                reason = "disk_floor"
                break
            if time.monotonic() >= deadline:
                reason = "runtime_limit"
                break
            while head is not None and safe_head is not None and next_index <= through_index \
                    and min(head, safe_head) >= games[next_index]["l2_block"]:
                try:
                    result = verify_index(config, next_index)
                    emit("game_verified", **result)
                    next_index += 1
                    check_failures = 0
                except Mismatch as exc:
                    emit("checkpoint_mismatch", game=next_index, error=str(exc)[:300])
                    reason = "checkpoint_mismatch"
                    break
                except AuditError as exc:
                    check_failures += 1
                    emit("checkpoint_retry", game=next_index,
                         failures=check_failures, error=str(exc)[:220])
                    if check_failures >= 5:
                        reason = "repeated_checkpoint_error"
                    break
                except Exception as exc:
                    emit("checkpoint_unexpected_error", game=next_index,
                         error=repr(exc)[:220])
                    reason = "unexpected_error"
                    break
            if fast_floor.is_set():
                reason = "disk_floor"
                break
            if reason in ("checkpoint_mismatch", "repeated_checkpoint_error",
                          "unexpected_error"):
                break
            if next_index > through_index:
                reason = "target_reached"
                break
            fast_floor.wait(20)
    except KeyboardInterrupt:
        reason = "disk_floor" if fast_floor.is_set() else "interrupted"
    finally:
        fast_floor.set()
        guarded_stop()
        emit("stopped", reason=reason, head=head, safe_head=safe_head,
             next_game=next_index,
             free_gib=round(shutil.disk_usage(config["data_dir"]).free / 2**30, 2))
    return 0 if reason == "target_reached" else 3 if reason == "disk_floor" else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--start-index", type=int, required=True)
    parser.add_argument("--through-index", type=int, required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--max-hours", type=float, default=24)
    args = parser.parse_args()
    if args.max_hours <= 0:
        parser.error("--max-hours must be positive")
    try:
        sys.exit(run(load_config(args.config), args.start_index,
                     args.through_index, args.log, args.max_hours))
    except AuditError as exc:
        parser.exit(2, f"monitor refused to start: {exc}\n")
