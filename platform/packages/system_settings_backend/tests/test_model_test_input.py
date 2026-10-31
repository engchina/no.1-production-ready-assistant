"""モデルのテストに送る共通の入力（#745）。"""

from __future__ import annotations

import base64

from pr_system_settings.model_test_input import (
    MODEL_TEST_IMAGE_BYTES,
    MODEL_TEST_IMAGE_MIME_TYPE,
    model_test_image_data_url,
    model_test_text_context,
)


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """JPEG の SOF から (幅, 高さ) を読む。"""
    index = 2
    while index < len(data):
        marker = data[index + 1]
        length = int.from_bytes(data[index + 2 : index + 4], "big")
        if 0xC0 <= marker <= 0xC3:
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            return width, height
        index += 2 + length
    raise AssertionError("SOF が見つかりません")


def test_model_test_image_is_jpeg_large_enough_for_providers() -> None:
    """1×1 の PNG のような小さい画像は gateway が拒否するため、512×512 の JPEG を使う。"""
    assert MODEL_TEST_IMAGE_MIME_TYPE == "image/jpeg"
    assert MODEL_TEST_IMAGE_BYTES.startswith(b"\xff\xd8")
    assert _jpeg_size(MODEL_TEST_IMAGE_BYTES) == (512, 512)


def test_model_test_image_data_url_matches_mime_type() -> None:
    prefix = "data:image/jpeg;base64,"
    data_url = model_test_image_data_url()

    assert data_url.startswith(prefix)
    assert base64.b64decode(data_url.removeprefix(prefix)) == MODEL_TEST_IMAGE_BYTES


def test_model_test_text_context_names_product() -> None:
    assert model_test_text_context("Production Ready Agent") == (
        "これは Production Ready Agent のモデル接続テスト用コンテキストです。"
    )
