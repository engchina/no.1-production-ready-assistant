"""cv2 が GUI の OS ライブラリなしで import できることの smoke test(#310)。

GUI 版の ``opencv-python`` は libxcb / libGL / glib に依存し、slim の base image では
``import cv2`` が ``ImportError: libxcb.so.1`` で失敗する。docling の TableFormer は PDF の
解析のたびに cv2 を import するため、失敗すると全 PDF が ``docling_adapter_failed`` になる。
本サービスは headless 版だけを入れ、GUI 版を入れない(pyproject の exclude-dependencies)。
"""

from __future__ import annotations

import importlib
import importlib.metadata

import pytest


def _installed(distribution: str) -> bool:
    try:
        importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def test_opencv_is_headless_only() -> None:
    assert _installed("opencv-python-headless")
    # GUI 版が同居すると cv2 パッケージを上書きし、GUI の OS ライブラリが必要になる。
    assert not _installed("opencv-python")


def test_import_cv2() -> None:
    cv2 = importlib.import_module("cv2")
    assert cv2.__version__


@pytest.mark.parametrize(
    "module",
    [
        # docling の StandardPdfPipeline が表構造モデルの初期化で import する。
        "docling_ibm_models.tableformer.data_management.tf_predictor",
        # Docling の解析の OCR(RapidOCR)も cv2 を import する。
        "rapidocr",
    ],
)
def test_cv2_dependent_modules_import(module: str) -> None:
    importlib.import_module(module)
