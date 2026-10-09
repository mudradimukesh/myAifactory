import argparse
import fcntl
import hashlib
import json
import re
import shlex
import time
from datetime import datetime
from pathlib import Path


def stamp(value, fallback=0):
    if not value:
        return fallback
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def poll(path, now=None):
    now = time.time() if now is None else now
    cache_path = path.with_name("watch-cursors.json")
    with path.with_name("watch-cursors.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        state = json.loads(path.read_text())
        alerts = inspect(path, state, cache, now)
        temporary = cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(cache, indent=2) + "\n")
        temporary.replace(cache_path)
        return alerts


def inspect(path, state, cache, now):
    alerts = []
    seen = cache.setdefault("emitted", [])

    def emit(check, evidence, lines, hypothesis, identity, **extra):
        key = hashlib.sha256(
            json.dumps([check, identity], sort_keys=True).encode()
        ).hexdigest()
        if key not in seen:
            seen.append(key)
            alerts.append(
                {
                    "check": check,
                    "evidence_path": str(evidence),
                    "line_range": lines,
                    "hypothesis": hypothesis,
                    **extra,
                }
            )

    pause = path.with_name("PAUSE")
    if pause.exists():
        emit(
            "W0",
            pause,
            [1, len(pause.read_text().splitlines())],
            "User pause requires immediate handling",
            pause.read_text(),
        )
    stage = "pilot" if state.get("pilot_started_at") else "preparation"
    remaining = sum(
        max(0, r["allowance"] - r["observed"])
        for r in state.get("reservations", {}).values()
        if not r.get("released")
    )
    for key in ("overall", stage):
        used = state.get("consumed", {}).get(key)
        ceiling = state.get("ceilings", {}).get(key)
        if used is not None and ceiling and used >= 0.9 * ceiling:
            emit(
                "W1",
                path,
                [1, 1],
                "Consumed usage requires budget reassessment",
                [key, ceiling],
                consumed=used,
                ceiling=ceiling,
                remaining_reservations=remaining,
            )
        deadline = state.get("deadlines", {}).get(key)
        if deadline:
            end = stamp(deadline)
            start = stamp(
                state.get("pilot_started_at")
                if key == "pilot"
                else state.get("started_at")
            )
            if now >= end or (start and now >= start + 0.9 * (end - start)):
                emit(
                    "W1",
                    path,
                    [1, 1],
                    "Applicable time threshold reached",
                    [key, deadline],
                )
    sources = {}
    lead_write = 0
    for group in ("threads", "sessions"):
        for row in state.get("inventory", {}).get(group, []):
            source = row.get("transcript_path") or row.get("rollout_path")
            if source and Path(source).is_file():
                sources[source] = row.get("role")
                if row.get("role") == "lead":
                    lead_write = max(lead_write, Path(source).stat().st_mtime)
    commands = cache.setdefault("commands", [])
    denials = cache.setdefault("denials", [])
    pending = cache.setdefault("pending_tools", {})
    boundaries = cache.get("boundaries", 0)
    for name, role in sources.items():
        source = Path(name)
        cursor = cache.setdefault("files", {}).setdefault(
            name, {"offset": 0, "line": 0}
        )
        if source.stat().st_size < cursor["offset"]:
            cursor.update(offset=0, line=0)
        with source.open("rb") as stream:
            stream.seek(cursor["offset"])
            while True:
                raw = stream.readline()
                if not raw.endswith(b"\n"):
                    break
                cursor["offset"] = stream.tell()
                cursor["line"] += 1
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                at = stamp(event.get("timestamp"), now)
                blocks = event.get("message", {}).get("content", [])
                if not isinstance(blocks, list):
                    continue
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    if kind == "tool_use" and role == "lead":
                        command = block.get("input", {}).get("command", "")
                        pending[block["id"]] = {
                            "command": command,
                            "name": block.get("name"),
                        }
                        if block.get("name") == "Agent" or any(
                            s in command for s in ("codex-agent.sh", "builder-start.sh")
                        ):
                            boundaries += 1
                        if command:
                            try:
                                normalized = shlex.join(shlex.split(command))
                            except ValueError:
                                normalized = command.strip()
                            commands.append([at, normalized, name, cursor["line"]])
                    elif kind == "tool_result":
                        pending.pop(block.get("tool_use_id"), None)
                        text = block.get("content", "")
                        if not isinstance(text, str):
                            text = json.dumps(text)
                        statuses = re.findall(
                            "(?m)^\\s*(FAIL|DIED|STALL_\\w+|CONTEXT|BUDGET|MODEL_DRIFT|EFFORT"
                            "_DRIFT)\\b[^\\n]*",
                            text,
                        )
                        if statuses:
                            emit(
                                "W2",
                                source,
                                [cursor["line"], cursor["line"]],
                                "Worker reported a failure or supervision alert",
                                [name, cursor["line"]],
                            )
                        if re.search(
                            r"PermissionError|EPERM|(?:auto.mode|fp-guard).*?(?:denied|blocked)",
                            text,
                            re.IGNORECASE,
                        ):
                            denials.append([at, name, cursor["line"]])
    commands[:] = [row for row in commands if now - row[0] <= 1800]
    denials[:] = [row for row in denials if now - row[0] <= 600]
    for command in {row[1] for row in commands}:
        rows = [row for row in commands if row[1] == command]
        if len(rows) >= 3:
            emit(
                "W4",
                rows[-1][2],
                [rows[0][3], rows[-1][3]],
                "Same normalized command ran three times within thirty minutes",
                [command, rows[0]],
            )
    if len(denials) >= 2:
        emit(
            "W5",
            denials[-1][1],
            [denials[0][2], denials[-1][2]],
            "Repeated permission or guard failures require diagnosis",
            denials[:2],
        )
    active = bool(pending) or any(
        r.get("active_run") for r in state.get("reservations", {}).values()
    )
    active |= any(
        s.get("role") == "reviewer" and not s.get("complete")
        for s in state.get("inventory", {}).get("sessions", [])
    )
    work_state = Path(state.get("work_state_path", path.parent / "state.md"))
    if (
        state.get("lifecycle") == "running"
        and not active
        and lead_write
        and now - lead_write >= 600
    ):
        text = work_state.read_text() if work_state.exists() else ""
        if re.search(
            "(?im)^next(?: step)?:\\s*(?:dispatch|run|prepare|resume|review|"
            "merge|implement|launch)\\b",
            text,
        ):
            emit(
                "W3",
                work_state,
                [1, len(text.splitlines())],
                "Lead is idle with a dispatchable next step",
                lead_write,
            )
    for run_id, row in state.get("runs", {}).items():
        receipt = path.with_name(run_id + "-launch.json")
        record = json.loads(receipt.read_text()) if receipt.exists() else row
        ended = stamp(record.get("ended_at"))
        if (
            record.get("status") == "REPLY"
            and ended
            and now - ended > 60
            and lead_write < ended
        ):
            emit(
                "W6",
                receipt if receipt.exists() else path,
                [1, 1],
                "Worker reply has waited over sixty seconds",
                run_id,
            )
    inbox = path.with_name("inbox.md")
    if inbox.exists():
        text = inbox.read_text()
        acked = set(re.findall(r"(?m)^ack\s+(\d+)\b", text))
        instructions = cache.setdefault("instructions", {})
        for number in re.findall(r"(?m)^(\d+)[.) ]", text):
            instructions.setdefault(number, boundaries)
            if number not in acked and boundaries - instructions[number] >= 2:
                emit(
                    "W6",
                    inbox,
                    [1, len(text.splitlines())],
                    "Instruction unacknowledged after two dispatch boundaries",
                    number,
                )
    failures = cache.setdefault("gate_failures", {})
    for gate_path in path.parent.glob("*-gate.json"):
        gate = json.loads(gate_path.read_text())
        identity = hashlib.sha256(gate_path.read_bytes()).hexdigest()
        if identity in cache.setdefault("gate_packets", []):
            continue
        cache["gate_packets"].append(identity)
        for row in gate.get("gates", []):
            log = Path(row.get("evidence", ""))
            if not row.get("exit_code") or not log.is_file():
                continue
            for node in re.findall(r"(?m)^FAILED\s+(\S+::\S+)", log.read_text()):
                key = json.dumps([gate.get("tree"), node])
                if key in failures:
                    emit(
                        "W4",
                        log,
                        [1, len(log.read_text().splitlines())],
                        "Same test failed in two gates without a tree change",
                        key,
                    )
                failures[key] = identity
    cache["boundaries"] = boundaries
    return alerts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args()
    end = time.monotonic() + args.timeout
    while True:
        alerts = poll(args.run)
        for alert in alerts:
            print(json.dumps(alert), flush=True)
        if alerts or time.monotonic() >= end or args.run.with_name("PAUSE").exists():
            return
        time.sleep(min(5, max(0, end - time.monotonic())))


if __name__ == "__main__":
    main()
