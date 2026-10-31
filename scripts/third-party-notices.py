#!/usr/bin/env python3
"""第三者の依存の一覧を lockfile から作り、THIRD-PARTY-NOTICES.md と突き合わせる（#3392）。

標準ライブラリだけで動く（runner の python3 で実行できる。3.11 以上の tomllib を使う）。

  python3 scripts/third-party-notices.py inventory [--repo DIR] [--all]
      git が管理する uv.lock / package-lock.json から、依存の一覧を CSV で出す
      （列: ecosystem,lockfile,name,version,scope,license）。
      - uv.lock: 自分の project（source が editable / virtual の ".")の dependencies と
        optional-dependencies（extra）から辿れる package を runtime / extra とし、dev-dependencies だけから
        辿れるものは出さない（--all で dev も出す）。uv.lock はライセンスを持たないので license の列は空。
      - package-lock.json: lockfile の dev の印で runtime / dev を分け、lockfile の license を出す。
        Vite は devDependencies のものも bundle に入れる（書体など）ため、dev の印は配布の有無を表さない。
        既定で dev も出す。

  python3 scripts/third-party-notices.py check [--repo DIR] [--notices FILE]
      THIRD-PARTY-NOTICES.md の表（1 列目が `名前`、2 列目が取得元の PyPI / npm の行。名前は `*` の glob でもよい）と
      lockfile を突き合わせる。
      - 表の PyPI / npm の部品が、どの lockfile にも無い（dev を含む依存から外れた）ときは失敗にする。
      - npm の package（dev を含む）で、ライセンスが PERMISSIVE に無いものが表に無いときは失敗にする。
      Python のライセンスは uv.lock に無いので、この検査では見ない（手順は THIRD-PARTY-NOTICES.md の
      「一覧の更新の手順」）。
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

# 表示の義務が著作権表示と許諾文の同梱だけで、THIRD-PARTY-NOTICES.md の表に個別に載せない SPDX の id。
PERMISSIVE = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BlueOak-1.0.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC-BY-4.0",
        "CC0-1.0",
        "ISC",
        "MIT",
        "MIT-0",
        "Python-2.0",
        "Unlicense",
        "WTFPL",
        "Zlib",
    }
)

NOTICE_ECOSYSTEMS = {"pypi": "python", "npm": "npm"}


@dataclass(frozen=True)
class Entry:
    ecosystem: str
    lockfile: str
    name: str
    version: str
    scope: str
    license: str


def normalize_python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def tracked_lockfiles(repo: Path) -> list[Path]:
    """git が管理する lockfile（作業用の node_modules・.venv の中のものを拾わない）。"""
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--", "*uv.lock", "*package-lock.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    names = ("uv.lock", "package-lock.json")
    return sorted(repo / line for line in out.splitlines() if Path(line).name in names)


def _dep_key(dep: dict, by_name: dict[str, list[dict]]) -> list[tuple[str, str]]:
    candidates = by_name.get(dep["name"], [])
    if "version" in dep:
        candidates = [p for p in candidates if p.get("version") == dep["version"]]
    return [(p["name"], p.get("version", "")) for p in candidates]


def uv_lock_entries(path: Path, repo: Path, include_dev: bool) -> Iterator[Entry]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    packages: list[dict] = data.get("package", [])
    by_name: dict[str, list[dict]] = {}
    for pkg in packages:
        by_name.setdefault(pkg["name"], []).append(pkg)
    by_key = {(p["name"], p.get("version", "")): p for p in packages}
    roots = [p for p in packages if "." in (p.get("source", {}).get("editable"), p.get("source", {}).get("virtual"))]

    seen: dict[tuple[str, str], str] = {}

    def walk(start: Iterable[dict], scope: str) -> None:
        visited: set[tuple[tuple[str, str], tuple[str, ...]]] = set()
        stack = list(start)
        while stack:
            dep = stack.pop()
            for key in _dep_key(dep, by_name):
                pkg = by_key[key]
                extras = tuple(dep.get("extra", ()))
                visit = (key, extras)
                if visit in visited:
                    continue
                visited.add(visit)
                seen.setdefault(key, scope)
                stack.extend(pkg.get("dependencies", []))
                for extra in extras:
                    stack.extend(pkg.get("optional-dependencies", {}).get(extra, []))

    for root in roots:
        walk(root.get("dependencies", []), "runtime")
        for extra, deps in sorted(root.get("optional-dependencies", {}).items()):
            walk(deps, f"extra:{extra}")
        if include_dev:
            for group, deps in sorted(root.get("dev-dependencies", {}).items()):
                walk(deps, f"dev:{group}")
    rel = path.relative_to(repo).as_posix()
    root_keys = {(p["name"], p.get("version", "")) for p in roots}
    for (name, version), scope in sorted(seen.items()):
        pkg = by_key[(name, version)]
        if (name, version) in root_keys or "registry" not in pkg.get("source", {}):
            continue  # 自分の project と、repo の中の package（editable / path）は第三者ではない。
        yield Entry("python", rel, name, version, scope, "")


def package_lock_entries(path: Path, repo: Path) -> Iterator[Entry]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rel = path.relative_to(repo).as_posix()
    for key, pkg in sorted(data.get("packages", {}).items()):
        if not key or pkg.get("link") or "node_modules/" not in key:
            continue  # 自分の package と、file: のリンク（platform の共有 package）は第三者ではない。
        if not pkg.get("resolved", "").startswith("https://"):
            continue  # workspace の中の package（platform/packages/*）。
        name = pkg.get("name") or key.rsplit("node_modules/", 1)[-1]
        scope = "dev" if pkg.get("dev") or pkg.get("devOptional") else "runtime"
        yield Entry("npm", rel, name, pkg.get("version", ""), scope, str(pkg.get("license", "")))


def inventory(repo: Path, include_dev: bool) -> list[Entry]:
    entries: list[Entry] = []
    for lock in tracked_lockfiles(repo):
        if lock.name == "uv.lock":
            entries.extend(uv_lock_entries(lock, repo, include_dev))
        else:
            entries.extend(package_lock_entries(lock, repo))
    return entries


def is_permissive(expression: str) -> bool:
    """SPDX の式が PERMISSIVE だけで満たせるか（OR はどれか 1 つ、AND はすべて）。"""
    expr = expression.strip().strip("()")
    if not expr:
        return False
    if " OR " in expr:
        return any(is_permissive(part) for part in expr.split(" OR "))
    if " AND " in expr:
        return all(is_permissive(part) for part in expr.split(" AND "))
    return expr in PERMISSIVE


def notice_rows(text: str) -> set[tuple[str, str]]:
    """表の 1 列目が `名前`（複数・`*` の glob も可）、2 列目が PyPI / npm の行から (ecosystem, 名前) を集める。"""
    rows: set[tuple[str, str]] = set()
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().split("|")]
        if len(cells) < 4 or cells[0] or not cells[1].startswith("`"):
            continue
        ecosystem = NOTICE_ECOSYSTEMS.get(cells[2].lower())
        if ecosystem is None:
            continue
        for name in re.findall(r"`([^`]+)`", cells[1]):
            rows.add((ecosystem, normalize_python_name(name) if ecosystem == "python" else name))
    return rows


def _key(entry: Entry) -> tuple[str, str]:
    if entry.ecosystem == "python":
        return ("python", normalize_python_name(entry.name))
    return ("npm", entry.name)


def check(repo: Path, notices: Path) -> list[str]:
    rows = notice_rows(notices.read_text(encoding="utf-8"))
    entries = inventory(repo, include_dev=True)
    present = {_key(e) for e in entries}

    def listed(key: tuple[str, str]) -> bool:
        return any(eco == key[0] and fnmatch.fnmatchcase(key[1], pattern) for eco, pattern in rows)

    errors = [
        f"{notices.name} の {eco} の部品 {pattern} が、どの lockfile の依存にもありません（表から外してください）"
        for eco, pattern in sorted(rows)
        if not any(k[0] == eco and fnmatch.fnmatchcase(k[1], pattern) for k in present)
    ]
    flagged: dict[str, set[str]] = {}
    for e in entries:
        if e.ecosystem == "npm" and not is_permissive(e.license) and not listed(_key(e)):
            flagged.setdefault(f"{e.name}（{e.license or 'ライセンスの記載なし'}）", set()).add(e.lockfile)
    for label, locks in sorted(flagged.items()):
        errors.append(
            f"npm の {label} が {notices.name} の表にありません: {', '.join(sorted(locks))}"
            "（ライセンスを確かめて表に足すか、PERMISSIVE に足してください）"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    sub = parser.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("inventory", help="lockfile の依存の一覧を CSV で出す")
    inv.add_argument("--all", action="store_true", help="uv.lock の dev-dependencies も出す")
    chk = sub.add_parser("check", help="THIRD-PARTY-NOTICES.md と lockfile を突き合わせる")
    chk.add_argument("--notices", type=Path, default=None)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()

    if args.command == "inventory":
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["ecosystem", "lockfile", "name", "version", "scope", "license"])
        for e in inventory(repo, include_dev=args.all):
            writer.writerow([e.ecosystem, e.lockfile, e.name, e.version, e.scope, e.license])
        return 0

    notices = args.notices or repo / "THIRD-PARTY-NOTICES.md"
    errors = check(repo, notices)
    for message in errors:
        print(f"error: {message}", file=sys.stderr)
    if errors:
        return 1
    print(f"ok: {notices.name} の表と lockfile が合っています")
    return 0


if __name__ == "__main__":
    sys.exit(main())
