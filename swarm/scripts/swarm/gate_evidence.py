import hashlib
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from run_state import ProcessInfo

POC_TEST_POLICY = "poc-single-pass-2026-10-21"
FINAL_ONLY = {"G10", "G12", "G13", "G18"}
POC_PROJECTS = ["light-keyboard", "light-pointer"]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output(command: Sequence[str], root: Path) -> str:
    return subprocess.check_output(command, cwd=root, text=True).strip()


def retire_worktree(repo, root, run_path=None):
    """Remove a finished worktree; return None, or why it was kept.

    Ignored files (.venv, node_modules, caches) are disposable. Tracked edits,
    untracked files, a live reservation on the path, or any process with its cwd or an
    open file inside keep it. If process inspection fails, ownership is unknown and the
    worktree is kept.
    """
    root = Path(root)
    if not root.exists():
        return None
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True
    )
    if status.returncode or status.stdout.strip():
        return "uncommitted changes" if not status.returncode else status.stderr.strip()
    inside = str(root.resolve())
    if run_path is not None:
        state = json.loads(Path(run_path).read_text())
        for key, row in state.get("reservations", {}).items():
            path = row.get("worktree")
            if path and not row.get("released") and Path(path).resolve().is_relative_to(inside):
                return "reservation " + key + " is not released"
    files = subprocess.run(["lsof", "-n", "-Fn"], capture_output=True, text=True)
    if files.returncode or not files.stdout:
        return "process inspection failed (lsof exit " + str(files.returncode) + ")"
    if any(
        line[1:] == inside or line[1:].startswith(inside + "/")
        for line in files.stdout.splitlines()
        if line.startswith("n")
    ):
        return "a process is using it"
    removed = subprocess.run(
        ["git", "worktree", "remove", "--force", str(root)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return removed.stderr.strip() or "git worktree remove failed" if removed.returncode else None


NODE = Path(
    os.environ.get(
        "PILOT_NODE",
        "/Users/mukeshkumarmudradi/.cache/codex-runtimes/codex-primary-"
        "runtime/dependencies/node/bin/node",
    )
)


def artifact_hashes(
    art: Path,
    tag: str,
    spec: Mapping[str, Sequence[str]],
    fast: bool = False,
    final: bool = False,
) -> dict[str, str]:
    required = [
        *spec.get("required_artifacts", []),
        *(spec.get("final_artifacts", []) if final else []),
    ]
    names = [
        "acceptance.md",
        "ids.txt",
        "test-map.json",
        "s1-tests.json",
        "s1-tests.sha256",
        "red.json",
        "fast-survivors.json" if fast else "final-survivors.json" if final else "survivors.json",
        *required,
    ]
    hashes = {}
    for name in names:
        path = art / (tag + "-" + name)
        if not path.resolve().is_relative_to(art.resolve()):
            raise ValueError("Artifact escapes packet directory")
        hashes[path.name] = digest(path)
        if name in required:
            manifest = json.loads(path.read_text())
            for item in manifest.get("artifacts", []):
                child = art / item["path"]
                if not child.resolve().is_relative_to(art.resolve()):
                    raise ValueError("Evidence escapes packet directory")
                actual = digest(child)
                if actual != item["sha256"]:
                    raise ValueError("Evidence hash differs")
                hashes[item["path"]] = actual
    return hashes


def fingerprint(root: Path) -> dict[str, str]:
    python = root / ".venv/bin/python"
    frozen = output([str(python), "-m", "pip", "freeze"], root)
    return {
        "python": output([str(python), "--version"], root),
        "pip_freeze_sha256": hashlib.sha256(frozen.encode()).hexdigest(),
        "node": output([str(NODE), "--version"], root),
        "package_lock_sha256": digest(root / "web/package-lock.json"),
        "os": platform.platform(),
    }


def acceptance(path: Path) -> dict[str, object]:
    text = path.read_text()
    blocks = text.split("```json\n")
    if len(blocks) != 2:
        raise ValueError("Acceptance needs exactly one JSON metadata block")
    metadata: dict[str, object] = json.loads(blocks[1].split("```", 1)[0])
    return metadata


def changed(root: Path, base: str, candidate: str) -> list[str]:
    return output(["git", "diff", "--name-only", base, candidate], root).splitlines()


def production(files: list[str], language: str) -> list[str]:
    if language == "python":
        return [
            f
            for f in files
            if f.startswith(("src/", "scripts/swarm/"))
            and f.endswith(".py")
            and "tests" not in Path(f).parts
            and not Path(f).name.startswith("test_")
            and not Path(f).name.endswith("_test.py")
        ]
    return [
        f
        for f in files
        if f.startswith("web/src/")
        and f.endswith((".ts", ".tsx"))
        and not f.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))
    ]


def provision(root: Path, log: Path) -> dict[str, str]:
    env = {**os.environ, "PATH": str(NODE.parent) + os.pathsep + os.environ["PATH"], "CI": "1"}
    with log.open("w") as stream:
        if not (root / ".venv/bin/python").exists():
            subprocess.run(
                [sys.executable, "-m", "venv", str(root / ".venv")],
                check=True,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        subprocess.run(
            [str(root / ".venv/bin/python"), "-m", "pip", "install", "-e", ".[dev,api]"],
            cwd=root,
            env=env,
            check=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        subprocess.run(
            ["npm", "ci", "--prefix", "web"],
            cwd=root,
            env=env,
            check=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    return env


@contextmanager
def runtime(root: Path, art: Path, tag: str, env: Mapping[str, str]) -> Iterator[dict[str, str]]:
    from owned_processes import capture, stop

    name = "p1_pilot_" + uuid4().hex[:16]
    subprocess.run(["createdb", "-h", "/tmp", "-O", "interior_migrator", name], check=True)
    configured = {
        **env,
        "PYTHONPATH": str(root / "src") + os.pathsep + env.get("PYTHONPATH", ""),
        "INTERIOR_IMAGE_URL_SIGNING_SECRET": secrets.token_urlsafe(32),
        "PILOT_ART": str(art),
        "PILOT_TAG": tag,
        "INTERIOR_DATABASE_URL": f"postgresql://app_runtime@/{name}?host=/tmp",
        "INTERIOR_MIGRATION_URL": f"postgresql://interior_migrator@/{name}?host=/tmp",
        "PILOT_TEST_DATABASE_URL": f"postgresql://app_runtime@/{name}?host=/tmp",
        "PILOT_TEST_MIGRATION_URL": f"postgresql://interior_migrator@/{name}?host=/tmp",
        "INTERIOR_STORE_DIR": str(root / ".pilot-store" / tag),
        "PILOT_EXPIRED_TOKEN_FILE": str(root / ".pilot-store" / tag / "expired-token.txt"),
    }
    api = socket.socket()
    api.bind(("127.0.0.1", 0))
    api.listen()
    port = api.getsockname()[1]
    with socket.socket() as preview:
        preview.bind(("127.0.0.1", 0))
        web_port = preview.getsockname()[1]
    configured.update(
        INTERIOR_JWKS_URL=f"http://127.0.0.1:{port}/idp/jwks.json",
        PILOT_API_URL=f"http://127.0.0.1:{port}",
        PILOT_WEB_PORT=str(web_port),
    )
    child = None
    owned: dict[int, ProcessInfo] = {}
    receipt: dict[str, object] = {
        "database": name,
        "api_port": port,
        "web_port": web_port,
        "root": str(root),
    }
    try:
        with (art / (tag + "-runtime.log")).open("w") as log:
            python = str(root / ".venv/bin/python")
            subprocess.run(
                [python, "-m", "alembic", "upgrade", "head"],
                cwd=root,
                env=configured,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
            code = (
                "import os; from pathlib import Path; import uvicorn; "
                "from floor_interior_design_and_renderer.api.app import create_app; "
                "from floor_interior_design_and_renderer.api.local_idp import L"
                "ocalIdentityProvider; "
                "from floor_interior_design_and_renderer.api.settings import Settings; "
                "settings=Settings.from_env(); provider=LocalIdentityProvider(s"
                "ettings.issuer, settings.audience, ttl_s=-3600); "
                "token=Path(os.environ['PILOT_EXPIRED_TOKEN_FILE']); token.pare"
                "nt.mkdir(parents=True,exist_ok=True); "
                "token.write_text(provider.issue('expired-pilot-account')); tok"
                "en.chmod(0o600); provider.ttl_s=3600; "
                "app=create_app(settings); app.mount('/idp',provider.asgi_app()); "
                f"uvicorn.run(app, fd={api.fileno()})"
            )
            child = subprocess.Popen(
                [python, "-c", code],
                cwd=root,
                env=configured,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(api.fileno(),),
            )
            capture(child.pid, owned)
            receipt["owned_processes"] = owned
            api.close()
            ready = False
            for _ in range(100):
                if child.poll() is not None:
                    raise ValueError("Isolated API exited before readiness")
                try:
                    with urllib.request.urlopen(
                        configured["INTERIOR_JWKS_URL"], timeout=1
                    ) as response:
                        ready = response.status == 200
                except OSError:
                    pass
                if ready:
                    break
                time.sleep(0.1)
            if not ready:
                raise ValueError("Isolated API did not become ready")
            yield configured
    finally:
        api.close()
        try:
            if child:
                signals, remaining = stop(child.pid, owned, reap=child.poll)
                receipt.update(signals=signals, remaining_processes=remaining)
                if remaining:
                    raise ValueError("Isolated API cleanup incomplete")
            # Drop only the database this runtime created, once its API has stopped.
            dropped = subprocess.run(["dropdb", "-h", "/tmp", name], capture_output=True, text=True)
            receipt["database_dropped"] = dropped.returncode == 0
            if dropped.returncode:
                raise ValueError("Isolated database cleanup failed: " + dropped.stderr.strip())
        finally:
            (art / (tag + "-runtime.json")).write_text(json.dumps(receipt, indent=2) + "\n")
