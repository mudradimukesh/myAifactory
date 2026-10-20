import os
import signal
import time
from collections.abc import Callable

from run_state import ProcessInfo, processes, same_identity


def capture(root: int, owned: dict[int, ProcessInfo]) -> dict[int, ProcessInfo]:
    table = processes()
    if root not in owned and root in table:
        owned[root] = table[root]
    changed = True
    while changed:
        changed = False
        for pid, row in table.items():
            parent = owned.get(row["ppid"])
            if pid not in owned and parent and same_identity(parent, table.get(parent["pid"])):
                owned[pid] = row
                changed = True
    return table


def survivors(
    owned: dict[int, ProcessInfo],
    table: dict[int, ProcessInfo] | None = None,
    uncertain: set[int] | None = None,
) -> list[int]:
    table = processes() if table is None else table
    result = []
    for pid, identity in owned.items():
        if pid not in table or table[pid].get("status", "").startswith("Z"):
            continue
        if not same_identity(identity, table[pid]):
            for _ in range(3):
                time.sleep(0.05)
                current = processes().get(pid)
                if current is None or current.get("status", "").startswith("Z"):
                    break
                if same_identity(identity, current):
                    result.append(pid)
                    break
            else:
                if uncertain is None:
                    raise ValueError(f"Uncertain process ownership for PID {pid}")
                uncertain.add(pid)
            continue
        result.append(pid)
    return result


def stop(
    root: int,
    owned: dict[int, ProcessInfo],
    int_wait: float = 30,
    term_wait: float = 15,
    reap: Callable[[], int | None] | None = None,
    *,
    descendants: bool = True,
) -> tuple[list[dict[str, object]], list[int]]:
    actions: list[dict[str, object]] = []
    uncertain: set[int] = set()
    for sig, delay in ((signal.SIGINT, int_wait), (signal.SIGTERM, term_wait), (signal.SIGKILL, 3)):
        if reap:
            reap()
        table = capture(root, owned) if descendants else processes()
        if reap and reap() is not None:
            table.pop(root, None)
        protected = {os.getpid()}
        pid = os.getpid()
        while pid in table and table[pid]["ppid"] not in protected:
            pid = table[pid]["ppid"]
            protected.add(pid)
        for pid in survivors(owned, table, uncertain):
            if pid in protected:
                raise ValueError("Refusing to signal controller or its ancestors")
            fresh = processes()
            if reap and reap() is not None:
                fresh.pop(root, None)
            if pid not in fresh or fresh[pid].get("status", "").startswith("Z"):
                continue
            if not same_identity(owned[pid], fresh[pid]):
                uncertain.add(pid)
                continue
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                continue
            actions.append({"pid": pid, "signal": sig.name, "at": time.time()})
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if reap:
                reap()
            table = capture(root, owned) if descendants else processes()
            if reap and reap() is not None:
                table.pop(root, None)
            if not survivors(owned, table, uncertain):
                break
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        else:
            continue
        break
    table = processes()
    live = {pid for pid in uncertain if pid in table and not table[pid]["status"].startswith("Z")}
    return actions, sorted(live.union(survivors(owned, table, uncertain)))
