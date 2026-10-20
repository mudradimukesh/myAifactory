import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import ExitStack, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TextIO

SERVICES = ("api", "jwks", "web")


@dataclass
class Receipt:
    allocation_id: str
    worktree: str
    ports: dict[str, int]
    pids: dict[str, int]
    database_url: str
    exports: dict[str, str]
    allocation_ids: list[str] = field(default_factory=list)
    detached_allocation_ids: list[str] = field(default_factory=list)


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("Expected a JSON object with string keys")
    return {key: item for key, item in value.items()}


def string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a string")
    return value


def integers(value: object) -> dict[str, int]:
    result = {}
    for key, item in mapping(value).items():
        if type(item) is not int or item <= 0:
            raise ValueError("Expected positive integer resources")
        result[key] = item
    if set(result) != set(SERVICES):
        raise ValueError("Expected api, jwks and web resources")
    return result


def receipt(value: object) -> Receipt:
    data = mapping(value)
    allocation_ids = data.get("allocation_ids", [data["allocation_id"]])
    detached_ids = data.get("detached_allocation_ids", [])
    if not isinstance(allocation_ids, list) or not isinstance(detached_ids, list):
        raise ValueError("Expected allocation id lists")
    return Receipt(
        string(data["allocation_id"]),
        string(data["worktree"]),
        integers(data["ports"]),
        integers(data["pids"]),
        string(data["database_url"]),
        {key: string(item) for key, item in mapping(data["exports"]).items()},
        [string(item) for item in allocation_ids],
        [string(item) for item in detached_ids],
    )


def save_registry(stream: TextIO, registry: dict[str, Receipt]) -> None:
    stream.seek(0)
    json.dump({name: asdict(item) for name, item in registry.items()}, stream)
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def pidfile(directory: Path, name: str, service: str) -> Path:
    return directory / name / f"{service}.pid"


def verify(directory: Path, name: str, item: Receipt, registry: dict[str, Receipt]) -> None:
    if item.allocation_id != name or item.database_url != database_url(name):
        raise ValueError("Allocation does not own the registered database")
    path = directory / f"{name}.json"
    if receipt(json.loads(path.read_text())) != item:
        raise ValueError("Receipt differs from the registered allocation resources")
    for service, pid in item.pids.items():
        if int(pidfile(directory, name, service).read_text()) != pid or pid <= 1:
            raise ValueError("Pidfile differs from the registered allocation resources")
    for other_name, other in registry.items():
        if other_name == name:
            continue
        if (
            set(item.pids.values()) & set(other.pids.values())
            or set(item.ports.values()) & set(other.ports.values())
            or item.database_url == other.database_url
        ):
            raise ValueError("Resources belong to another allocation")


def database_url(name: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if sanitized != name or len(sanitized) > 60:
        sanitized = sanitized[:47] + "_" + hashlib.sha256(name.encode()).hexdigest()[:12]
    return f"postgresql://app_runtime@/p1_{sanitized}?host=/tmp"


def stop(pids: dict[str, int], children: list[subprocess.Popen[bytes]]) -> None:
    def has_members(pid: int) -> bool:
        if sys.platform != "darwin":
            return True
        import ctypes

        list_group = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_listpids
        list_group.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
        list_group.restype = ctypes.c_int
        member = ctypes.c_int()
        ctypes.set_errno(0)
        proc_pgrp_only = 2
        count = int(list_group(proc_pgrp_only, pid, ctypes.byref(member), ctypes.sizeof(member)))
        return count != 0 or ctypes.get_errno() != 0

    for sig, delay in ((signal.SIGTERM, 10), (signal.SIGKILL, 3)):
        pending = set(pids.values())
        for pid in pids.values():
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                pending.discard(pid)
            except PermissionError:
                if not has_members(pid):
                    pending.discard(pid)
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            for child in children:
                child.poll()
            groups_alive = False
            for pid in pending:
                try:
                    os.killpg(pid, 0)
                    groups_alive = True
                except ProcessLookupError:
                    pass
                except PermissionError:
                    if has_members(pid):
                        groups_alive = True
            if not groups_alive:
                return
            time.sleep(0.1)
    raise ValueError("Owned processes survived shutdown")


def drop(name: str) -> None:
    database = database_url(name).split("/p1_", 1)[1].split("?", 1)[0]
    subprocess.run(["dropdb", "-h", "/tmp", f"p1_{database}"], check=True)


def remove(directory: Path, name: str, item: Receipt, registry: dict[str, Receipt]) -> None:
    verify(directory, name, item, registry)
    stop(item.pids, [])
    drop(name)
    for service in SERVICES:
        pidfile(directory, name, service).unlink()
    (directory / f"{name}.json").unlink()
    del registry[name]


def health(item: Receipt) -> None:
    if not all(alive(pid) for pid in item.pids.values()):
        raise ValueError("Registered environment has dead processes; use down before up")
    with urllib.request.urlopen(item.exports["INTERIOR_JWKS_URL"], timeout=2) as response:
        if response.status != 200 or not mapping(json.loads(response.read())).get("keys"):
            raise ValueError("JWKS is unhealthy")
    with urllib.request.urlopen(
        f"http://127.0.0.1:{item.ports['web']}/bench", timeout=2
    ) as response:
        if response.status != 200:
            raise ValueError("Preview is unhealthy")
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{item.ports['api']}/api/me", timeout=2).close()
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return
        raise
    raise ValueError("API did not require authentication")


def start(directory: Path, name: str, root: Path, registry: dict[str, Receipt]) -> Receipt:
    if not (root / "web/playwright.config.ts").is_file():
        raise ValueError("Worktree does not contain the web preview configuration")
    if name in registry:
        existing = registry[name]
        verify(directory, name, existing, registry)
        if existing.worktree != str(root):
            raise ValueError("Allocation is registered to another worktree")
        health(existing)
        return existing
    dependencies = root / "web/node_modules"
    if not dependencies.is_dir():
        raise ValueError(f"Missing {dependencies}; run npm ci in {root / 'web'}")
    owned = directory / name
    owned.mkdir(exist_ok=True)
    children: list[subprocess.Popen[bytes]] = []
    pids: dict[str, int] = {}
    ports: dict[str, int] = {}
    created = False
    try:
        with ExitStack() as stack:
            sockets = {}
            used = {port for item in registry.values() for port in item.ports.values()}
            for service in SERVICES:
                listener = stack.enter_context(socket.socket())
                while True:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                    if port not in used:
                        break
                    listener.close()
                    listener = stack.enter_context(socket.socket())
                listener.listen()
                ports[service] = port
                used.add(port)
                sockets[service] = listener
            exports = {
                "INTERIOR_DATABASE_URL": database_url(name),
                "INTERIOR_MIGRATION_URL": database_url(name).replace(
                    "app_runtime@", "interior_migrator@"
                ),
                "PILOT_TEST_DATABASE_URL": database_url(name),
                "PILOT_TEST_MIGRATION_URL": database_url(name).replace(
                    "app_runtime@", "interior_migrator@"
                ),
                "INTERIOR_JWKS_URL": f"http://127.0.0.1:{ports['jwks']}/idp/jwks.json",
                "PILOT_API_URL": f"http://127.0.0.1:{ports['api']}",
                "PILOT_WEB_PORT": str(ports["web"]),
                "PILOT_TASK_ENV_PREVIEW": "1",
                "INTERIOR_STORE_DIR": str(owned / "store"),
                "PILOT_EXPIRED_TOKEN_FILE": str(owned / "expired-token.txt"),
            }
            env = {**os.environ, **exports}
            env["INTERIOR_IMAGE_URL_SIGNING_SECRET"] = secrets.token_urlsafe(32)
            env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
            python_path = root / ".venv/bin/python"
            python = str(python_path) if python_path.is_file() else sys.executable
            log = stack.enter_context((owned / "runtime.log").open("ab"))
            subprocess.run(
                [
                    "createdb",
                    "-h",
                    "/tmp",
                    "-O",
                    "interior_migrator",
                    database_url(name).split("@/", 1)[1].split("?", 1)[0],
                ],
                check=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            created = True
            subprocess.run(
                [python, "-m", "alembic", "upgrade", "head"],
                cwd=root,
                env=env,
                check=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            subprocess.run(
                ["npm", "run", "build"],
                cwd=root / "web",
                env=env,
                check=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            codes = {
                "api": (
                    "import os; import httpx; import uvicorn; "
                    "from fastapi import Request; from starlette.responses import Response; "
                    "from floor_interior_design_and_renderer.api.app import create_app; "
                    "from floor_interior_design_and_renderer.api.settings import Settings; "
                    "app=create_app(Settings.from_env())\n"
                    "@app.api_route('/idp/{path:path}', methods=['GET', 'POST'])\n"
                    "async def idp(request: Request, path: str) -> Response:\n"
                    "    async with httpx.AsyncClient(trust_env=False) as client:\n"
                    "        response = await client.request(request.method, "
                    "os.environ['INTERIOR_JWKS_URL'].removesuffix('/jwks.json') + '/' + path, "
                    "params=request.query_params, content=await request.body(), "
                    "headers={'content-type': request.headers.get('content-type', '')})\n"
                    "    headers = {key: value for key, value in response.headers.items() "
                    "if key not in {'content-length', 'content-encoding'}}\n"
                    "    return Response(response.content, "
                    "status_code=response.status_code, headers=headers)\n"
                ),
                "jwks": (
                    "import os; from pathlib import Path; import uvicorn; "
                    "from fastapi import FastAPI; "
                    "from floor_interior_design_and_renderer.api.local_idp "
                    "import LocalIdentityProvider; "
                    "from floor_interior_design_and_renderer.api.settings import Settings; "
                    "settings=Settings.from_env(); provider=LocalIdentityProvider("
                    "settings.issuer, settings.audience, ttl_s=-3600); "
                    "token=Path(os.environ['PILOT_EXPIRED_TOKEN_FILE']); "
                    "token.write_text(provider.issue('expired-pilot-account')); "
                    "token.chmod(0o600); provider.ttl_s=3600; "
                    "app=FastAPI(); app.mount('/idp',provider.asgi_app()); "
                ),
            }
            for service, code in codes.items():
                listener = sockets[service]
                child = subprocess.Popen(
                    [python, "-c", code + f"uvicorn.run(app, fd={listener.fileno()})"],
                    cwd=root,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    pass_fds=(listener.fileno(),),
                )
                children.append(child)
                pids[service] = child.pid
                pidfile(directory, name, service).write_text(str(child.pid) + "\n")
                listener.close()
            sockets["web"].close()
            child = subprocess.Popen(
                ["npm", "run", "preview", "--", "--port", str(ports["web"]), "--strictPort"],
                cwd=root / "web",
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            children.append(child)
            pids["web"] = child.pid
            pidfile(directory, name, "web").write_text(str(child.pid) + "\n")
            item = Receipt(name, str(root), ports, pids, database_url(name), exports)
            deadline = time.monotonic() + 30
            while True:
                if any(child.poll() is not None for child in children):
                    raise ValueError(
                        f"Service exited before readiness; see {owned / 'runtime.log'}"
                    )
                try:
                    health(item)
                    break
                except (OSError, ValueError) as error:
                    if time.monotonic() >= deadline:
                        raise ValueError(
                            f"Services did not become ready; see {owned / 'runtime.log'}"
                        ) from error
                    time.sleep(0.1)
            (directory / f"{name}.json").write_text(json.dumps(asdict(item)) + "\n")
            registry[name] = item
            return item
    except BaseException:
        with suppress(Exception):
            stop(pids, children)
        if created:
            with suppress(Exception):
                drop(name)
        for service in pids:
            with suppress(Exception):
                pidfile(directory, name, service).unlink(missing_ok=True)
        with suppress(Exception):
            (directory / f"{name}.json").unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up")
    up.add_argument("allocation_id")
    up.add_argument("worktree", type=Path)
    down = sub.add_parser("down")
    down.add_argument("allocation_id")
    sub.add_parser("status")
    sub.add_parser("reap")
    args = parser.parse_args()
    name = args.allocation_id if args.command in {"up", "down"} else ""
    if name and not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError("Allocation id must contain only letters, digits, underscores and hyphens")
    run = Path(os.environ["PILOT_RUN"])
    reservations = mapping(mapping(json.loads(run.read_text())).get("reservations", {}))
    allocation = name
    reservation_units = {}
    for reservation_id, value in reservations.items():
        reservation = mapping(value)
        if "worktree" in reservation:
            reserved_root = Path(string(reservation["worktree"])).resolve()
            basename = re.sub(r"[^a-zA-Z0-9_]", "_", reserved_root.name)[:47]
            key = basename + "_" + hashlib.sha256(str(reserved_root).encode()).hexdigest()[:12]
        else:
            key = reservation_id
        reservation_units[reservation_id] = key
    name = reservation_units.get(allocation, allocation)
    if args.command == "up":
        if allocation not in reservations:
            parser.exit(1, f"Allocation {allocation} has no registered reservation\n")
        reservation = mapping(reservations[allocation])
        root = args.worktree.resolve()
        if "worktree" in reservation:
            reserved_root = Path(string(reservation["worktree"])).resolve()
            if reserved_root != root:
                parser.exit(1, f"Allocation {allocation} reservation records another worktree\n")
    directory = run.resolve().parent / "task-env"
    directory.mkdir(exist_ok=True)
    descriptor = os.open(directory / "registry.json", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        content = stream.read()
        registry = {
            key: receipt(value) for key, value in mapping(json.loads(content or "{}")).items()
        }
        if args.command == "up":
            try:
                item = start(directory, name, root, registry)
            except ValueError as error:
                parser.exit(1, f"{error}\n")
            if allocation not in item.allocation_ids:
                item.allocation_ids.append(allocation)
            if allocation in item.detached_allocation_ids:
                item.detached_allocation_ids.remove(allocation)
            (directory / f"{name}.json").write_text(json.dumps(asdict(item)) + "\n")
            save_registry(stream, registry)
            print(json.dumps(asdict(item)))
        elif args.command == "down":
            for key, item in list(registry.items()):
                if not (
                    key == name
                    or allocation in item.allocation_ids
                    or allocation in item.detached_allocation_ids
                ):
                    continue
                verify(directory, key, item, registry)
                if allocation in item.detached_allocation_ids:
                    break
                peers = [
                    peer
                    for peer, unit in reservation_units.items()
                    if unit == key
                    and peer != allocation
                    and peer not in item.detached_allocation_ids
                    and (
                        mapping(reservations[peer]).get("released") is not True
                        or mapping(reservations[peer]).get("active_run") is not None
                    )
                ]
                if peers:
                    item.detached_allocation_ids.append(allocation)
                    item.allocation_ids = peers
                    (directory / f"{key}.json").write_text(json.dumps(asdict(item)) + "\n")
                else:
                    remove(directory, key, item, registry)
                save_registry(stream, registry)
                break
            else:
                if (directory / f"{name}.json").exists():
                    raise ValueError("Receipt has no registered allocation")
        elif args.command == "status":
            print(
                json.dumps(
                    {
                        key: asdict(item)
                        for key, item in registry.items()
                        if all(alive(pid) for pid in item.pids.values())
                    }
                )
            )
        else:
            for key, item in list(registry.items()):
                unit_reservations = [
                    mapping(value)
                    for allocation_id, value in reservations.items()
                    if reservation_units[allocation_id] == key
                ]
                if (
                    unit_reservations
                    and all(
                        value.get("released") is True and value.get("active_run") is None
                        for value in unit_reservations
                    )
                    and not any(alive(pid) for pid in item.pids.values())
                ):
                    remove(directory, key, item, registry)
                    save_registry(stream, registry)


if __name__ == "__main__":
    main()
