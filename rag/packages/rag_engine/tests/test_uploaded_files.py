"""アップロード済み PDF の一覧・再選択・削除を保護するテスト。"""

import json
import os
import shutil
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from rag_engine.parsing.parse_inputs import PARSE_INPUT_DIRECTORY, filename_key
from rag_engine.workflows.uploaded_files import (
    delete_uploaded_file,
    find_uploaded_file,
    list_uploaded_files,
    materialize_uploaded_file,
)


def _run(root: Path, run_id: str, name: str, content: bytes, *, mtime: float, chunk_run_id: str = "", vision: bool | None = None) -> Path:
    """run を 1 つ作る。vision が None 以外なら、その値で解析済み（results.json あり）にする。"""
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "source.pdf").write_bytes(content)
    (run_dir / "viewer-data.json").write_text(json.dumps({"run_id": run_id, "pdf_name": name, "source_page_count": 3}), encoding="utf-8")
    if vision is not None:
        (run_dir / "results.json").write_text(
            json.dumps({"run_id": run_id, "statuses": [{"engine": "docling", "available": True, "use_docling_vision": vision}]}),
            encoding="utf-8",
        )
    if chunk_run_id:
        (run_dir / "chunks" / chunk_run_id).mkdir(parents=True)
    os.utime(run_dir / "source.pdf", (mtime, mtime))
    return run_dir


class UploadedFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "runs"
        self.root.mkdir()
        # 同じ合成内容の複製と削除が、別 worker・別テストの一時領域へ波及しないようにする。
        temp_root = patch("rag_engine.workflows.uploaded_files.tempfile.gettempdir", return_value=self.tmp.name)
        temp_root.start()
        self.addCleanup(temp_root.stop)

    def test_lists_runs_grouped_by_content_and_name_newest_first_with_limit(self):
        _run(self.root, "aaaaaa01", "manual.pdf", b"manual", mtime=100)
        _run(self.root, "aaaaaa02", "manual.pdf", b"manual", mtime=300, chunk_run_id="c0ffee01")  # 同じファイルの再解析
        _run(self.root, "bbbbbb01", "other.pdf", b"other", mtime=200)
        _run(self.root, "cccccc01", "manual.pdf", b"manual v2", mtime=50)  # 同名でも内容が違えば別ファイル
        _run(self.root, "dddddd01", "scan.png", b"image", mtime=400)  # PDF 以外は一覧に出さない
        (self.root / "_cache").mkdir()
        (self.root / "eeeeee01").mkdir()  # source.pdf のない壊れた run

        files = list_uploaded_files(self.root)

        self.assertEqual(
            [(f.file_name, f.run_ids) for f in files],
            [("manual.pdf", ("aaaaaa02", "aaaaaa01")), ("other.pdf", ("bbbbbb01",)), ("manual.pdf", ("cccccc01",))],
        )
        self.assertEqual(files[0].chunk_run_ids, ("c0ffee01",))
        self.assertIn("manual.pdf（3 ページ / チャンク済み", files[0].label)
        self.assertIn("チャンク未作成", files[1].label)
        # 一覧の表の列: ファイル名・ページ数・サイズ・解析・AI 読み取り・チャンク・Embedding。
        # 実行回数・最終更新は出さない。embedded_sources を渡さないときは Embedding の有無を判定しない（未確認）。
        self.assertEqual(files[0].columns, ["manual.pdf", "3 ページ", "1 KB", "未解析", "-", "チャンク済み", "未確認"])
        self.assertEqual(files[1].columns[3:], ["未解析", "-", "未作成", "未確認"])
        # ADB の (SHA-256, ファイル名) と一致するファイルだけが「保存済み」。同名で内容の違うファイルは一致しない。
        embedded = list_uploaded_files(self.root, embedded_sources={(files[0].sha256, "manual.pdf")})
        self.assertEqual([f.columns[6] for f in embedded], ["保存済み", "未保存", "未保存"])
        self.assertEqual(len({f.key for f in files}), 3)
        self.assertEqual([f.file_name for f in list_uploaded_files(self.root, limit=1)], ["manual.pdf"])
        # 上限の外にあるファイルも選択値から引ける。
        self.assertEqual(find_uploaded_file(self.root, files[2].key), files[2])
        self.assertIsNone(find_uploaded_file(self.root, "../../etc/passwd"))

    def test_shows_whether_the_latest_analysis_used_vision(self):
        # 同じファイルを再解析して Vision を切り替えた場合は、最新の解析 run の設定を出す。
        _run(self.root, "aaaaaa01", "manual.pdf", b"manual", mtime=100, vision=True)
        _run(self.root, "aaaaaa02", "manual.pdf", b"manual", mtime=300, vision=False)
        _run(self.root, "bbbbbb01", "other.pdf", b"other", mtime=200, vision=True)
        _run(self.root, "cccccc01", "third.pdf", b"third", mtime=50)  # 未解析

        files = list_uploaded_files(self.root)

        self.assertEqual(
            [(f.file_name, f.analyzed, f.vision) for f in files],
            [("manual.pdf", True, False), ("other.pdf", True, True), ("third.pdf", False, None)],
        )
        self.assertEqual([f.columns[4] for f in files], ["なし", "あり", "-"])

    def test_materialized_file_keeps_the_original_name(self):
        # run 内は source.pdf 固定。元の名前で渡さないと保存済みの解析結果・チャンクの復元が働かない。
        _run(self.root, "aaaaaa01", "手順書 v2.pdf", b"manual", mtime=100)
        path = materialize_uploaded_file(self.root, list_uploaded_files(self.root)[0])
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        self.assertEqual(path.name, "手順書 v2.pdf")
        self.assertEqual(path.read_bytes(), b"manual")
        self.assertNotIn(str(self.root), str(path))
        self.assertTrue(path.is_relative_to(Path(self.tmp.name)))

    def test_delete_isolated_by_workspace_and_filename(self):
        for separate_workspace, other_name in [(True, "manual.pdf"), (True, "other.pdf"), (False, "other.pdf")]:
            with self.subTest(separate_workspace=separate_workspace, other_name=other_name):
                root_a = Path(self.tmp.name) / f"a-{separate_workspace}-{other_name}"
                root_b = Path(self.tmp.name) / f"b-{other_name}" if separate_workspace else root_a
                _run(root_a, "aaaaaa01", "manual.pdf", b"same", mtime=100)
                _run(root_b, "bbbbbb01", other_name, b"same", mtime=200)
                target_a = next(x for x in list_uploaded_files(root_a) if x.file_name == "manual.pdf")
                target_b = next(x for x in list_uploaded_files(root_b) if x.file_name == other_name)
                path_a = materialize_uploaded_file(root_a, target_a)
                path_b = materialize_uploaded_file(root_b, target_b)
                self.assertNotEqual(path_a.parent, path_b.parent)
                delete_uploaded_file(root_a, target_a, SimpleNamespace(adb_settings=None))
                self.assertFalse(path_a.exists())
                self.assertEqual(path_b.read_bytes(), b"same")
                self.assertTrue((root_b / "bbbbbb01" / "source.pdf").is_file())

    def test_copy_and_delete_of_one_file_are_serialized(self):
        _run(self.root, "aaaaaa01", "manual.pdf", b"same", mtime=100)
        uploaded = list_uploaded_files(self.root)[0]
        copying, release, deleting = Event(), Event(), Event()
        original_copy = shutil.copyfile

        def copy(source, target):
            copying.set()
            if not release.wait(5):
                raise TimeoutError("複製の再開待ち")
            return original_copy(source, target)

        def delete_database(*args, **kwargs):
            deleting.set()
            return 1

        with ThreadPoolExecutor(max_workers=2) as pool, patch("rag_engine.workflows.uploaded_files.shutil.copyfile", side_effect=copy):
            copied = pool.submit(materialize_uploaded_file, self.root, uploaded)
            self.assertTrue(copying.wait(5))
            removed = pool.submit(
                delete_uploaded_file, self.root, uploaded, SimpleNamespace(adb_settings=object()), delete_documents=delete_database
            )
            try:
                self.assertFalse(deleting.wait(0.1))
            finally:
                release.set()
            path = copied.result(timeout=5)
            removed.result(timeout=5)
        self.assertTrue(deleting.is_set())
        self.assertFalse(path.exists())
        # 削除済みの原本から複製を復活させない。
        with self.assertRaises(FileNotFoundError):
            materialize_uploaded_file(self.root, uploaded)

    def test_delete_removes_database_rows_first_then_every_run_and_parse_input(self):
        _run(self.root, "aaaaaa01", "manual.pdf", b"manual", mtime=100)
        _run(self.root, "aaaaaa02", "manual.pdf", b"manual", mtime=300)
        keep = _run(self.root, "bbbbbb01", "other.pdf", b"other", mtime=200)
        parse_input = self.root / PARSE_INPUT_DIRECTORY / filename_key("manual.pdf")
        parse_input.mkdir(parents=True)
        other_parse_input = self.root / PARSE_INPUT_DIRECTORY / filename_key("other.pdf")
        other_parse_input.mkdir(parents=True)
        target = list_uploaded_files(self.root)[0]
        calls = []

        def delete_documents(settings, **kwargs):
            # ADB の削除時点では、ローカルの run がまだ残っている。
            calls.append((kwargs, (self.root / "aaaaaa01").is_dir()))
            return 1

        result = delete_uploaded_file(self.root, target, SimpleNamespace(adb_settings=object()), delete_documents=delete_documents)

        self.assertEqual((result.file_name, result.run_count, result.database_deleted), ("manual.pdf", 2, 1))
        self.assertEqual(calls[0][0]["source_file_sha256"], target.sha256)
        self.assertEqual(calls[0][0]["source_file_name"], "manual.pdf")
        self.assertTrue(calls[0][1])
        self.assertFalse((self.root / "aaaaaa01").exists() or (self.root / "aaaaaa02").exists() or parse_input.exists())
        self.assertTrue(keep.is_dir() and other_parse_input.is_dir())
        self.assertEqual([f.file_name for f in list_uploaded_files(self.root)], ["other.pdf"])

    def test_delete_skips_run_directories_that_are_already_gone(self):
        # 前回の削除が途中で失敗した後や、一覧取得後に別セッションで消えた run があっても、残りを消して完了する (#827)。
        _run(self.root, "aaaaaa01", "manual.pdf", b"manual", mtime=100)
        _run(self.root, "aaaaaa02", "manual.pdf", b"manual", mtime=300)
        parse_input = self.root / PARSE_INPUT_DIRECTORY / filename_key("manual.pdf")
        parse_input.mkdir(parents=True)
        target = list_uploaded_files(self.root)[0]
        shutil.rmtree(self.root / "aaaaaa01")

        result = delete_uploaded_file(self.root, target, SimpleNamespace(adb_settings=None))

        self.assertEqual((result.file_name, result.run_count), ("manual.pdf", 1))
        self.assertFalse((self.root / "aaaaaa02").exists() or parse_input.exists())

    def test_database_failure_keeps_local_files_so_the_delete_can_be_retried(self):
        _run(self.root, "aaaaaa01", "manual.pdf", b"manual", mtime=100)
        target = list_uploaded_files(self.root)[0]

        def failing(settings, **kwargs):
            raise RuntimeError("ORA-12541")

        with self.assertRaisesRegex(RuntimeError, "ORA-12541"):
            delete_uploaded_file(self.root, target, SimpleNamespace(adb_settings=object()), delete_documents=failing)
        self.assertTrue((self.root / "aaaaaa01" / "source.pdf").is_file())

    def test_without_database_settings_only_local_files_are_deleted(self):
        _run(self.root, "aaaaaa01", "manual.pdf", b"manual", mtime=100)
        result = delete_uploaded_file(
            self.root,
            list_uploaded_files(self.root)[0],
            SimpleNamespace(adb_settings=None),
            delete_documents=lambda *a, **k: self.fail("ADB 未設定では呼ばない"),
        )
        self.assertIsNone(result.database_deleted)
        self.assertFalse((self.root / "aaaaaa01").exists())

    def test_delete_refuses_run_ids_outside_the_output_directory(self):
        from dataclasses import replace

        _run(self.root, "aaaaaa01", "manual.pdf", b"manual", mtime=100)
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        forged = replace(list_uploaded_files(self.root)[0], run_ids=("../outside",))
        with self.assertRaises(ValueError):
            delete_uploaded_file(self.root, forged, SimpleNamespace(adb_settings=None))
        self.assertTrue(outside.is_dir())


if __name__ == "__main__":
    unittest.main()
