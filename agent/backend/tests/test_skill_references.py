"""スキルの参照文書（SKILL.md の隣の `references/`）と `skill_reference_read`（#1803）。

Agent Skills の progressive disclosure の第 3 段: 参照文書は指示へ詰めず、必要なときだけ読む。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from app.features.agent import skills_loader
from app.features.agent.skill_references import (
    REFERENCE_CHUNK_CHARACTERS,
    reference_tool,
    skill_reference_keys,
)
from app.features.agent.skills import skill_registry
from app.features.agent.skills_loader import load_skill_references, load_skills_from_dir
from app.features.agent.tools import ToolInvocationContext

SKILL_ID = "ref1803-skill"


def _write_skill(base: Path, skill_id: str = SKILL_ID) -> Path:
    skill_dir = base / skill_id
    (skill_dir / "references" / "nested").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: 参照文書つきのスキル。\n"
        "metadata:\n  production-ready-display-name: 参照文書つき\n---\n手順の本文\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "guide.md").write_text("ガイドの本文", encoding="utf-8")
    (skill_dir / "references" / "nested" / "terms.txt").write_text("用語", encoding="utf-8")
    # テキスト / Markdown 以外と、references の外のファイルは読まない（scripts は実行もしない）。
    (skill_dir / "references" / "logo.png").write_bytes(b"\x89PNG")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "run.sh").write_text("echo x", encoding="utf-8")
    return skill_dir


@pytest.fixture
def registered() -> Iterator[None]:
    yield
    skill_registry.set_declared("project", [])


def test_loader_reads_references_as_skill_references(tmp_path: Path) -> None:
    _write_skill(tmp_path)

    [skill] = load_skills_from_dir(str(tmp_path))

    assert skill.instructions == "手順の本文"
    assert [(item.path, item.characters, item.content) for item in skill.references] == [
        ("references/guide.md", 6, "ガイドの本文"),
        ("references/nested/terms.txt", 2, "用語"),
    ]
    # 本文は API の応答・保存（model_dump）に出さず、パスと文字数だけを出す。
    assert skill.model_dump(mode="json")["references"] == [
        {"path": "references/guide.md", "characters": 6},
        {"path": "references/nested/terms.txt", "characters": 2},
    ]


def test_loader_does_not_follow_symlinks_outside_the_skill(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path / "skills")
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "secret.md").write_text("外の文書", encoding="utf-8")
    (skill_dir / "references" / "linked.md").symlink_to(outside / "secret.md")
    (skill_dir / "references" / "linked_dir").symlink_to(outside, target_is_directory=True)

    paths = [item.path for item in load_skill_references(skill_dir)]

    assert paths == ["references/guide.md", "references/nested/terms.txt"]


def test_loader_ignores_symlinked_references_dir(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "doc.md").write_text("外", encoding="utf-8")
    (skill_dir / "references").symlink_to(outside, target_is_directory=True)

    assert load_skill_references(skill_dir) == []


def test_loader_limits_file_count_and_size(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    skill_dir = tmp_path / "skill"
    (skill_dir / "references").mkdir(parents=True)
    for index in range(3):
        (skill_dir / "references" / f"{index}.md").write_text("x" * 10, encoding="utf-8")
    (skill_dir / "references" / "big.md").write_text("x" * 50, encoding="utf-8")
    monkeypatch.setattr(skills_loader, "REFERENCE_MAX_FILE_BYTES", 20)
    monkeypatch.setattr(skills_loader, "REFERENCE_MAX_FILES", 2)

    paths = [item.path for item in load_skill_references(skill_dir)]

    # 1 ファイルの上限を超えた big.md と、件数の上限を超えた 2.md は読まない。
    assert paths == ["references/0.md", "references/1.md"]

    monkeypatch.setattr(skills_loader, "REFERENCE_MAX_FILES", 100)
    monkeypatch.setattr(skills_loader, "REFERENCE_MAX_TOTAL_BYTES", 25)
    assert [item.path for item in load_skill_references(skill_dir)] == [
        "references/0.md",
        "references/1.md",
    ]


def test_reference_tool_reads_in_chunks_only_assigned_references(
    tmp_path: Path, registered: None
) -> None:
    skill_dir = _write_skill(tmp_path)
    long_text = "あ" * (REFERENCE_CHUNK_CHARACTERS + 5)
    (skill_dir / "references" / "guide.md").write_text(long_text, encoding="utf-8")
    skill_registry.set_declared("project", load_skills_from_dir(str(tmp_path)))

    keys = skill_reference_keys([SKILL_ID, "missing-skill"])
    assert keys == [
        (SKILL_ID, "references/guide.md"),
        (SKILL_ID, "references/nested/terms.txt"),
    ]
    _, read = reference_tool(keys)
    context = ToolInvocationContext()

    first = read({"skill_id": SKILL_ID, "path": "references/guide.md", "offset": 0}, context)
    assert isinstance(first, dict)
    assert first["content"] == "あ" * REFERENCE_CHUNK_CHARACTERS
    assert first["next_offset"] == REFERENCE_CHUNK_CHARACTERS
    second = read(
        {"skill_id": SKILL_ID, "path": "references/guide.md", "offset": first["next_offset"]},
        context,
    )
    assert isinstance(second, dict)
    assert second["content"] == "あ" * 5
    assert second["next_offset"] is None

    for arguments in (
        # 割り当てていない文書・ディレクトリの外・本文以外は読まない。
        {"skill_id": SKILL_ID, "path": "../secret.md", "offset": 0},
        {"skill_id": SKILL_ID, "path": "scripts/run.sh", "offset": 0},
        {"skill_id": "other", "path": "references/guide.md", "offset": 0},
        {"skill_id": SKILL_ID, "path": "references/guide.md", "offset": -1},
    ):
        with pytest.raises(ValueError):
            read(arguments, context)


def test_disabled_skill_has_no_reference_keys(tmp_path: Path, registered: None) -> None:
    _write_skill(tmp_path)
    [skill] = load_skills_from_dir(str(tmp_path))
    skill_registry.set_declared("project", [skill.model_copy(update={"enabled": False})])

    assert skill_reference_keys([SKILL_ID]) == []
