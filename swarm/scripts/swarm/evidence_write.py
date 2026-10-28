#!/usr/bin/env python3
"""Write user-authorized gate evidence in the run folder ART (default: the P1 POC run folder).

s1 TAG TESTS PREDECESSOR [--red TAG-red.json] [--correction NOTE]
    Write TAG-s1-tests.sha256 and TAG-s1-tests.json for every test file that
    changes between PREDECESSOR and TESTS, backing up existing records under
    logs/ and restoring them if publication fails. Without --red the whole
    range must be test-only. --red must name a passed red receipt in ART for
    the same tests, predecessor and current TAG-test-map.json; then only the
    TESTS commit itself must be test-only.
diff TAG BASE HEAD
    Write TAG-diff.patch from git diff BASE HEAD.
hold TAG ATTEMPT
    Move TAG-* files that differ from attempts/TAG/ATTEMPT to logs/TAG-held/
    and restore the archived copies, so prepare_candidate can archive them.
release-held TAG
    Move the held files back.
"""

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

ART = Path(os.environ.get("ART", "/tmp/factory-fp-0925/swarm/interior-p1-poc"))
REPO = Path(os.environ.get("REPO") or Path(__file__).resolve().parents[2])
TEST_PATHS = ("tests/", "web/tests/", "web/e2e/")
LINEAGE = "Amendment lineage: "
ROOT_KEYS = ("predecessor", "tests_commit", "red_receipt", "red_sha256", "test_map_sha256")
AMENDMENT_KEYS = (
    "parent_inventory_sha256",
    "predecessor",
    "tests_commit",
    "preservation_receipt",
    "preservation_sha256",
    "assertion_equivalence",
    "assertion_equivalence_sha256",
    "owner_approval",
    "owner_approval_sha256",
)


def git(*args: str, repo: Path = REPO) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout


def load(path: Path, what: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"{what} {path} is unreadable: {error}") from None
    if not isinstance(value, dict):
        raise SystemExit(f"{what} {path} is not a JSON object")
    return value


def map_digest(tag: str) -> str:
    test_map = ART / f"{tag}-test-map.json"
    if not test_map.is_file():
        raise SystemExit(f"Test map missing: {test_map}")
    return hashlib.sha256(test_map.read_bytes()).hexdigest()


def _require_locations() -> None:
    if not ART.is_dir():
        raise SystemExit(f"ART missing: {ART}")
    inside = (
        REPO.is_dir()
        and subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=REPO, capture_output=True, text=True
        ).stdout.strip()
        == "true"
    )
    if not inside:
        raise SystemExit(f"REPO {REPO} is not a git work tree")


def _require_test_only(args: argparse.Namespace, tests: str, changed: list[str]) -> None:
    if args.red:
        tip = git("diff-tree", "--no-commit-id", "--name-only", "-r", tests).decode().split()
        if not tip or any(not f.startswith(TEST_PATHS) for f in tip):
            raise SystemExit("Tests commit must change only test files: " + " ".join(tip))
    elif not changed or any(not f.startswith(TEST_PATHS) for f in changed):
        raise SystemExit("Tests commit must change only test files: " + " ".join(changed))


class FrozenRange(NamedTuple):
    tests: str
    predecessor: str
    files: list[str]


def _frozen_range(args: argparse.Namespace) -> FrozenRange:
    tests = git("rev-parse", args.tests).decode().strip()
    predecessor = git("rev-parse", args.predecessor).decode().strip()
    changed = git("diff-tree", "-r", "--name-only", predecessor, tests).decode().split()
    _require_test_only(args, tests, changed)
    scope = ("--", *TEST_PATHS)
    files = git("diff-tree", "-r", "--name-only", predecessor, tests, *scope).decode().split()
    deleted = (
        git("diff-tree", "-r", "--name-only", "--diff-filter=D", predecessor, tests, *scope)
        .decode()
        .split()
    )
    if deleted:
        raise SystemExit("Deleted test paths cannot be frozen: " + " ".join(deleted))
    if not files:
        raise SystemExit(f"No test files change between {predecessor} and {tests}")
    return FrozenRange(tests, predecessor, files)


def _red_test_map(args: argparse.Namespace, frozen: FrozenRange, lineage: bool) -> str | None:
    if not args.red:
        return None
    if args.red != f"{args.tag}-red.json":
        raise SystemExit(f"Red receipt must be {args.tag}-red.json, the name the gate reads")
    if not (ART / args.red).is_file():
        raise SystemExit(f"Red receipt missing: {ART / args.red}")
    red = load(ART / args.red, "Red receipt")
    tests, predecessor = frozen.tests, frozen.predecessor
    bound = lineage or (red.get("tests_commit"), red.get("predecessor")) == (tests, predecessor)
    if red.get("passed") is not True or not bound:
        raise SystemExit(f"Red receipt {args.red} is not a pass for {tests} on {predecessor}")
    test_map = red.get("test_map_sha256")
    if not isinstance(test_map, str):
        raise SystemExit(f"Red receipt {args.red} has no test map digest (test_map_sha256)")
    if test_map != map_digest(args.tag):
        raise SystemExit(f"Test map {args.tag}-test-map.json differs from red receipt")
    return test_map


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rows(inventory: dict[str, str]) -> bytes:
    return "".join(f"{inventory[f]}  {f}\n" for f in sorted(inventory)).encode()


def _relation(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) for k in keys):
        raise ValueError(LINEAGE + "malformed relation")
    return value


def _proof(art: Path, name: object, expected: object, reason: str) -> dict[str, object]:
    try:
        if str(name) != Path(str(name)).name:
            raise ValueError(reason)
        data = (art / str(name)).read_bytes()
        value = json.loads(data)
    except (OSError, UnicodeDecodeError, ValueError):
        raise ValueError(LINEAGE + reason) from None
    if _sha(data) != expected or not isinstance(value, dict):
        raise ValueError(LINEAGE + reason)
    return value


def _inventory(repo: Path, predecessor: str, tests: str) -> dict[str, str]:
    scope = ("--", *TEST_PATHS)
    files = git("diff-tree", "-r", "--name-only", predecessor, tests, *scope, repo=repo)
    return {f: _sha(git("show", f"{tests}:{f}", repo=repo)) for f in files.decode().split()}


def _blob(repo: Path, commit: str, path: str) -> str | None:
    if not git("ls-tree", commit, "--", path, repo=repo).strip():
        return None
    return _sha(git("show", f"{commit}:{path}", repo=repo))


def _root(repo: Path, art: Path, tag: str, root: dict[str, object]) -> dict[str, str]:
    red = _proof(art, root["red_receipt"], root["red_sha256"], "invalid root")
    inventory = _inventory(repo, str(root["predecessor"]), str(root["tests_commit"]))
    bound = all(red.get(k) == root[k] for k in ("predecessor", "tests_commit", "test_map_sha256"))
    if (
        red.get("passed") is not True
        or not bound
        or root["red_receipt"] != f"{tag}-red.json"
        or _sha(_rows(inventory)) != root.get("inventory_sha256")
    ):
        raise ValueError(LINEAGE + "invalid root")
    return inventory


def _proofs(
    art: Path, amendment: dict[str, object], parent: str, test_map: object
) -> dict[str, object]:
    preservation = _proof(
        art,
        amendment["preservation_receipt"],
        amendment["preservation_sha256"],
        "preservation digest differs",
    )
    expected = {
        "passed": True,
        "predecessor": parent,
        "tests_commit": amendment["tests_commit"],
        "test_map_sha256": test_map,
    }
    if any(preservation.get(k) != v for k, v in expected.items()):
        raise ValueError(LINEAGE + "preservation tuple differs")
    equivalence = _proof(
        art,
        amendment["assertion_equivalence"],
        amendment["assertion_equivalence_sha256"],
        "assertion proof differs",
    )
    return equivalence


def _assertions(repo: Path, commit: str, paths: list[str]) -> list[str]:
    found: list[str] = []
    for path in paths:
        if not path.endswith(".py"):
            raise ValueError(LINEAGE + "assertion proof differs")
        try:
            tree = ast.parse(git("show", f"{commit}:{path}", repo=repo))
        except (subprocess.CalledProcessError, SyntaxError, ValueError):
            raise ValueError(LINEAGE + "assertion proof differs") from None
        found += [ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Assert)]
    return found


def _preserved(
    repo: Path, amendment: dict[str, object], equivalence: dict[str, object], paths: list[str]
) -> None:
    before = _assertions(repo, str(amendment["predecessor"]), paths)
    after = _assertions(repo, str(amendment["tests_commit"]), paths)
    claimed = (equivalence.get("assertions_before"), equivalence.get("assertions_after"))
    if not before or before != after or claimed != (before, after):
        raise ValueError(LINEAGE + "assertion proof differs")


def _approval(
    art: Path, tag: str, amendment: dict[str, object], approved: str | None = None
) -> None:
    if approved is not None and amendment.get("owner_approval_sha256") != approved:
        raise ValueError(LINEAGE + "unapproved amendment")
    approval = _proof(
        art, amendment["owner_approval"], amendment["owner_approval_sha256"], "unapproved amendment"
    )
    body = {
        k: v for k, v in amendment.items() if k not in ("owner_approval", "owner_approval_sha256")
    }
    bound = _sha(json.dumps(body, sort_keys=True).encode())
    if approval.get("owner") != tag or approval.get("amendment_sha256") != bound:
        raise ValueError(LINEAGE + "unapproved amendment")


def _paths(repo: Path, amendment: dict[str, object], inventory: dict[str, str]) -> list[str]:
    parent, tests = str(amendment["predecessor"]), str(amendment["tests_commit"])
    paths = amendment.get("paths")
    dependencies = amendment.get("source_dependencies")
    if not isinstance(paths, dict) or not isinstance(dependencies, list):
        raise ValueError(LINEAGE + "malformed relation")
    for dependency in dependencies:
        try:
            git("merge-base", "--is-ancestor", str(dependency), parent, repo=repo)
        except subprocess.CalledProcessError:
            raise ValueError(LINEAGE + "source dependency differs") from None
    changed = git("diff-tree", "-r", "--name-only", parent, tests, repo=repo).decode().split()
    if set(changed) != set(paths) or any(not f.startswith(TEST_PATHS) for f in changed):
        raise ValueError(LINEAGE + "unlisted test path")
    for path, hashes in paths.items():
        before, after = _blob(repo, parent, path), _blob(repo, tests, path)
        if not isinstance(hashes, dict) or after is None:
            raise ValueError(LINEAGE + "malformed relation")
        if (hashes.get("before_sha256"), hashes.get("after_sha256")) != (before, after):
            raise ValueError(LINEAGE + "amendment paths differ")
        inventory[path] = after
    return sorted(paths)


def _chain(
    repo: Path, art: Path, tag: str, record: dict[str, object], approved: str | None
) -> bytes:
    lineage = _relation(record.get("lineage"), ())
    root = _relation(lineage.get("root"), ROOT_KEYS)
    amendments = lineage.get("amendments")
    if lineage.get("version") != 1 or not isinstance(amendments, list):
        raise ValueError(LINEAGE + "malformed relation")
    if root["predecessor"] != record.get("predecessor"):
        raise ValueError(LINEAGE + "invalid root")
    inventory = _root(repo, art, tag, root)
    parent = str(root["tests_commit"])
    for item in amendments:
        amendment = _relation(item, AMENDMENT_KEYS)
        if amendment["predecessor"] != parent or amendment["parent_inventory_sha256"] != _sha(
            _rows(inventory)
        ):
            raise ValueError(LINEAGE + "wrong parent inventory")
        equivalence = _proofs(art, amendment, parent, root["test_map_sha256"])
        _approval(art, tag, amendment, approved)
        _preserved(repo, amendment, equivalence, _paths(repo, amendment, inventory))
        parent = str(amendment["tests_commit"])
    final = _inventory(repo, str(root["predecessor"]), str(record.get("tests_commit")))
    if set(final) - set(inventory):
        raise ValueError(LINEAGE + "unlisted test path")
    if final != inventory:
        raise ValueError(LINEAGE + "final inventory differs")
    if parent != record.get("tests_commit"):
        raise ValueError(LINEAGE + "tests tuple differs")
    return _rows(final)


def validate_lineage(
    repo: Path, art: Path, tag: str, record: dict[str, object], approved: str | None = None
) -> bytes:
    try:
        return _chain(repo, art, tag, record, approved)
    except subprocess.CalledProcessError:
        raise ValueError(LINEAGE + "unknown commit") from None


def _read_prior(record: Path, hashes: Path) -> tuple[dict[Path, bytes], dict[str, object], object]:
    if record.exists() != hashes.exists():
        present, absent = (record, hashes) if record.exists() else (hashes, record)
        raise SystemExit(f"Incomplete s1 pair: {present} exists without {absent}")
    prior = {path: path.read_bytes() for path in (record, hashes) if path.exists()}
    old = load(record, "s1 record") if prior else {}
    previous = old.get("tests_commit")
    if prior and not isinstance(previous, str):
        raise SystemExit(f"s1 record {record} has no tests_commit")
    return prior, old, previous


def _plan_backups(prior: dict[Path, bytes], previous: object) -> list[tuple[Path, Path]]:
    backups = [(path, ART / "logs" / f"{path.name}.{str(previous)[:7]}.bak") for path in prior]
    for path, backup in backups:
        if backup.exists() and backup.read_bytes() != prior[path]:
            raise SystemExit(f"Backup already exists with different content: {backup}")
    for target in {ART, *prior, *(backup.parent for _, backup in backups)}:
        if not os.access(target, os.W_OK):
            raise SystemExit(f"{target} is not writable")
    return backups


def _build_record(
    args: argparse.Namespace,
    old: dict[str, object],
    frozen: FrozenRange,
    test_map: str | None,
) -> dict[str, object]:
    old.update(
        tests_commit=frozen.tests,
        predecessor=frozen.predecessor,
        test_map=old.get("test_map", f"{args.tag}-test-map.json"),
    )
    for key, value in (("frozen_files", frozen.files), ("red_receipt", args.red)):
        if key in old and value:
            old[key] = value
    old.update({k: v for k, v in (("red", args.red), ("correction", args.correction)) if v})
    if args.red:
        old["test_map_sha256"] = test_map
    return old


def _stage(outputs: dict[Path, str], staged: dict[Path, Path], made: list[Path]) -> None:
    for path, text in outputs.items():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            fd = os.open(staged[path], flags, 0o666)
        except FileExistsError:
            raise SystemExit(f"Staging path {staged[path]} already exists") from None
        made.append(staged[path])
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        if path.exists():
            shutil.copymode(path, staged[path])
        if staged[path].read_text() != text:
            raise SystemExit(f"Staged {staged[path]} does not hold the new {path.name}")


def _back_up(
    backups: list[tuple[Path, Path]], prior: dict[Path, bytes], created: list[Path]
) -> None:
    for path, backup in backups:
        if not backup.exists():
            created.append(backup)
            shutil.copy2(path, backup)
        if backup.read_bytes() != prior[path]:
            raise SystemExit(f"Backup {backup} does not hold the prior {path.name}")


def _require_unchanged(
    tag: str, outputs: dict[Path, str], prior: dict[Path, bytes], test_map: str | None
) -> None:
    if test_map is not None and test_map != map_digest(tag):
        raise SystemExit(f"Test map {tag}-test-map.json changed while writing")
    for path in outputs:
        if (path.read_bytes() if path.exists() else None) != prior.get(path):
            raise SystemExit(f"{path} changed while writing")


def _rollback(
    error: BaseException,
    published: list[Path],
    prior: dict[Path, bytes],
    backups: list[tuple[Path, Path]],
    created: list[Path],
) -> str:
    try:
        for path in published:
            if path in prior:
                path.write_bytes(prior[path])
            else:
                path.unlink()
    except OSError as restore:
        return (
            f"s1 failed ({error}) and restoring {published} failed ({restore}); "
            f"prior bytes are in {[str(backup) for _, backup in backups]}"
        )
    failure = f"s1 not written, prior records kept: {error}"
    for backup in created:
        try:
            backup.unlink(missing_ok=True)
        except OSError as cleanup:
            failure += f"; removing backup {backup} failed ({cleanup})"
    return failure


def _remove_staged(made: list[Path], failure: str | None, published: list[Path]) -> None:
    problems = []
    for temp in made:
        try:
            temp.unlink(missing_ok=True)
        except OSError as cleanup:
            problems.append(f"removing staging file {temp} failed ({cleanup})")
    if problems:
        summary = failure or f"s1 published {[str(path) for path in published]}"
        raise SystemExit("; ".join([summary, *problems])) from None


def _publish(
    tag: str,
    outputs: dict[Path, str],
    prior: dict[Path, bytes],
    backups: list[tuple[Path, Path]],
    test_map: str | None,
) -> None:
    staged = {path: ART / f".{path.name}.staged" for path in outputs}
    created: list[Path] = []
    made: list[Path] = []
    published: list[Path] = []
    failure: str | None = None
    try:
        _stage(outputs, staged, made)
        _back_up(backups, prior, created)
        _require_unchanged(tag, outputs, prior, test_map)
        for path in outputs:
            os.replace(staged[path], path)
            published.append(path)
    except (OSError, SystemExit) as error:
        failure = _rollback(error, published, prior, backups, created)
        raise SystemExit(failure) from None
    finally:
        _remove_staged(made, failure, published)


def s1(args: argparse.Namespace) -> None:
    _require_locations()
    frozen = _frozen_range(args)
    tests, _, files = frozen
    record, hashes = (ART / f"{args.tag}-s1-tests.{ext}" for ext in ("json", "sha256"))
    prior, old, previous = _read_prior(record, hashes)
    test_map = _red_test_map(args, frozen, "lineage" in old)
    backups = _plan_backups(prior, previous)
    rows = "".join(
        f"{hashlib.sha256(git('show', f'{tests}:{f}')).hexdigest()}  {f}\n" for f in files
    )
    old = _build_record(args, old, frozen, test_map)
    if "lineage" in old:
        if not args.approval_sha256:
            raise SystemExit(LINEAGE + "unapproved amendment")
        try:
            validate_lineage(REPO, ART, args.tag, old, args.approval_sha256)
        except ValueError as error:
            raise SystemExit(str(error)) from None
    _publish(args.tag, {hashes: rows, record: json.dumps(old)}, prior, backups, test_map)
    for _, backup in backups:
        print("backup", backup)
    print("wrote", record, hashes, len(files), "files")


def diff(args: argparse.Namespace) -> None:
    patch = ART / f"{args.tag}-diff.patch"
    patch.write_bytes(git("diff", args.base, args.head))
    print("wrote", patch)


def hold(args: argparse.Namespace) -> None:
    archive = ART / "attempts" / args.tag / args.attempt
    held = ART / "logs" / f"{args.tag}-held"
    held.mkdir(exist_ok=True)
    for saved in archive.iterdir():
        path = ART / saved.name
        if path.is_file() and path.read_bytes() != saved.read_bytes():
            shutil.move(path, held / path.name)
            shutil.copy2(saved, path)
            print("held", path.name)


def release_held(args: argparse.Namespace) -> None:
    held = ART / "logs" / f"{args.tag}-held"
    for path in held.iterdir():
        shutil.move(path, ART / path.name)
        print("restored", path.name)
    held.rmdir()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    commands = parser.add_subparsers(required=True)
    command = commands.add_parser("s1")
    command.add_argument("tag")
    command.add_argument("tests")
    command.add_argument("predecessor")
    command.add_argument("--red")
    command.add_argument("--correction")
    command.add_argument("--approval-sha256")
    command.set_defaults(run=s1)
    command = commands.add_parser("diff")
    command.add_argument("tag")
    command.add_argument("base")
    command.add_argument("head")
    command.set_defaults(run=diff)
    command = commands.add_parser("hold")
    command.add_argument("tag")
    command.add_argument("attempt")
    command.set_defaults(run=hold)
    command = commands.add_parser("release-held")
    command.add_argument("tag")
    command.set_defaults(run=release_held)
    args = parser.parse_args()
    args.run(args)
