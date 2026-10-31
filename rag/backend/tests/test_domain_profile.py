"""廃止した外部 DomainProfile のファイルを import 時も実行時も読まない。"""

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.rag.answer_engine import build_engine_settings


@pytest.mark.parametrize(
    "content",
    [
        None,
        "{broken",
        '{"aliases": [["設定", ["FILE_ONLY_ALIAS"]]], "categories": {"分類A": ["分類B"]}}',
    ],
    ids=["missing", "invalid", "valid"],
)
@pytest.mark.parametrize("explicit", [False, True], ids=["cwd", "env"])
def test_retired_files_are_never_opened_at_import_or_execution(
    tmp_path: Path, content: str | None, explicit: bool
) -> None:
    target = tmp_path / ("explicit.json" if explicit else "domain_profile.json")
    if content is not None:
        target.write_text(content, encoding="utf-8")
    env = dict(os.environ)
    env.pop("RAG_ENGINE_DOMAIN_PROFILE_FILE", None)
    if explicit:
        env["RAG_ENGINE_DOMAIN_PROFILE_FILE"] = str(target)
    code = """
import builtins
import json
from pathlib import Path
original_read = Path.read_text
original_open = builtins.open

def guard(path):
    if Path(path).name in {"domain_profile.json", "explicit.json"}:
        raise AssertionError("廃止したファイルを読んだ")

def read(path, *args, **kwargs):
    guard(path)
    return original_read(path, *args, **kwargs)

def open_file(path, *args, **kwargs):
    if isinstance(path, (str, Path)):
        guard(path)
    return original_open(path, *args, **kwargs)

Path.read_text = read
builtins.open = open_file
from rag_engine.knowledge import classification as classification
from rag_engine.retrieval.inquiry_conditions import parse_inquiry_conditions
from rag_engine.composition import create_oracle_application
from rag_engine.config import get_settings

parsed = parse_inquiry_conditions("sample.pdf の2ページの E1234 の確認手順は？")
assert parsed.has_signals
assert parsed.business_domains == ()
assert "E1234" in parsed.codes_and_errors
assert "FILE_ONLY_ALIAS" not in parsed.search_terms
saved = classification.classification_from_metadata(
    {"large_category": "10_分類A", "middle_category": "分類B"}
)
assert saved.large_category == "10_分類A"
assert saved.middle_category == "分類B"
assert classification.infer_classification_from_path("分類A/manual.pdf").large_category == ""
settings = get_settings(environ={}, dotenv_path=None)
assert not hasattr(settings, "profile")
with create_oracle_application(Path.cwd(), settings=settings) as application:
    with application.runtime.activate():
        assert parse_inquiry_conditions("設定を変更する手順は？").has_signals
        assert not hasattr(application.runtime, "profile")
print(json.dumps({"ok": True}))
"""
    process = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {"ok": True}
    assert target.exists() is (content is not None)


def test_retired_env_settings_are_not_part_of_answer_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = build_engine_settings(Settings(), output_dir=tmp_path)
    monkeypatch.setenv("RAG_ENGINE_DOMAIN_PROFILE_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("RAG_ENGINE_PROFILE", "legacy")
    monkeypatch.setenv("RAG_ANSWER_PROFILE", "legacy")
    after = build_engine_settings(Settings(), output_dir=tmp_path)
    assert dataclasses.asdict(before) == dataclasses.asdict(after)
    assert "rag_answer_profile" not in Settings.model_fields
    assert not hasattr(after, "profile")
