#!/usr/bin/env python3
"""Write user-authorized gate evidence in the P1 POC run folder.

s1 TAG TESTS PREDECESSOR [--red NOTE] [--correction NOTE]
    Write TAG-s1-tests.sha256 and TAG-s1-tests.json for the test files the
    tests commit changes, backing up existing records under logs/.
diff TAG BASE HEAD
    Write TAG-diff.patch from git diff BASE HEAD.
hold TAG ATTEMPT
    Move TAG-* files that differ from attempts/TAG/ATTEMPT to logs/TAG-held/
    and restore the archived copies, so prepare_candidate can archive them.
release-held TAG
    Move the held files back.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ART = Path("/tmp/factory-fp-0925/swarm/interior-p1-poc")
REPO = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True).stdout


def s1(args):
    tests = git("rev-parse", args.tests).decode().strip()
    files = git("diff-tree", "-r", "--name-only", args.predecessor, tests).decode().split()
    if not files or any(not f.startswith(("tests/", "web/tests/", "web/e2e/")) for f in files):
        raise SystemExit("Tests commit must change only test files: " + " ".join(files))
    record, hashes = (ART / f"{args.tag}-s1-tests.{ext}" for ext in ("json", "sha256"))
    old = json.loads(record.read_text()) if record.exists() else {}
    for path in (record, hashes):
        if path.exists():
            backup = ART / "logs" / f"{path.name}.{old['tests_commit'][:7]}.bak"
            shutil.copy2(path, backup)
            print("backup", backup)
    hashes.write_text(
        "".join(f"{hashlib.sha256(git('show', f'{tests}:{f}')).hexdigest()}  {f}\n" for f in files)
    )
    old.update(
        tests_commit=args.tests,
        predecessor=git("rev-parse", args.predecessor).decode().strip(),
        test_map=old.get("test_map", f"{args.tag}-test-map.json"),
    )
    old.update({k: v for k, v in (("red", args.red), ("correction", args.correction)) if v})
    record.write_text(json.dumps(old))
    print("wrote", record, hashes, len(files), "files")


def diff(args):
    patch = ART / f"{args.tag}-diff.patch"
    patch.write_bytes(git("diff", args.base, args.head))
    print("wrote", patch)


def hold(args):
    archive = ART / "attempts" / args.tag / args.attempt
    held = ART / "logs" / f"{args.tag}-held"
    held.mkdir(exist_ok=True)
    for saved in archive.iterdir():
        path = ART / saved.name
        if path.is_file() and path.read_bytes() != saved.read_bytes():
            shutil.move(path, held / path.name)
            shutil.copy2(saved, path)
            print("held", path.name)


def release_held(args):
    held = ART / "logs" / f"{args.tag}-held"
    for path in held.iterdir():
        shutil.move(path, ART / path.name)
        print("restored", path.name)
    held.rmdir()


parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
commands = parser.add_subparsers(required=True)
command = commands.add_parser("s1")
command.add_argument("tag")
command.add_argument("tests")
command.add_argument("predecessor")
command.add_argument("--red")
command.add_argument("--correction")
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
