import hashlib
import json
import os
import platform
import subprocess
from contextlib import contextmanager
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output(command, root):
    return subprocess.check_output(command, cwd=root, text=True).strip()


NODE = Path(os.environ.get("PILOT_NODE", __import__("shutil").which("node") or "node"))


def project_adapter():
    import importlib.util

    path = os.environ.get("SWARM_PROJECT_ADAPTER")
    if not path or not Path(path).is_file():
        raise ValueError(
            "Set SWARM_PROJECT_ADAPTER to the target project's tested runtime adapter"
        )
    spec = importlib.util.spec_from_file_location("swarm_project_adapter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def artifact_hashes(art, tag, spec):
    names = [
        "acceptance.md",
        "ids.txt",
        "test-map.json",
        "s1-tests.json",
        "s1-tests.sha256",
        "red.json",
        "survivors.json",
        *spec.get("required_artifacts", []),
    ]
    hashes = {}
    for name in names:
        path = art / (tag + "-" + name)
        if not path.resolve().is_relative_to(art.resolve()):
            raise ValueError("Artifact escapes packet directory")
        hashes[path.name] = digest(path)
        if name in spec.get("required_artifacts", []):
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


def fingerprint(root):
    python = root / ".venv/bin/python"
    frozen = output([str(python), "-m", "pip", "freeze"], root)
    return {
        "python": output([str(python), "--version"], root),
        "pip_freeze_sha256": hashlib.sha256(frozen.encode()).hexdigest(),
        "node": output([str(NODE), "--version"], root),
        "package_lock_sha256": digest(root / "web/package-lock.json"),
        "os": platform.platform(),
    }


def acceptance(path):
    text = path.read_text()
    blocks = text.split("```json\n")
    if len(blocks) != 2:
        raise ValueError("Acceptance needs exactly one JSON metadata block")
    return json.loads(blocks[1].split("```", 1)[0])


def changed(root, base, candidate):
    return output(["git", "diff", "--name-only", base, candidate], root).splitlines()


def production(files, language):
    if language == "python":
        return [f for f in files if f.startswith("src/") and f.endswith(".py")]
    return [
        f for f in files if f.startswith("web/src/") and f.endswith((".ts", ".tsx"))
    ]


def provision(root, log):
    return project_adapter().provision(root, log)


@contextmanager
def runtime(root, art, tag, env):
    with project_adapter().runtime(root, art, tag, env) as configured:
        yield configured
