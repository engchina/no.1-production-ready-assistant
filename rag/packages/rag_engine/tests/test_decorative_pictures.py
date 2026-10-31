"""解析時の Picture 分類が、節見出しの再掲と著作権表示だけの帯画像を decorative にする (#899)。"""

from rag_engine.parsing.decorative_pictures import classify_picture_record

BANNER_BBOX = [50, 300, 550, 340]


def _record(id, category, page, seq, text="", *, raw_type=None, bbox=None):
    return {
        "id": id,
        "engine": "docling",
        "page": page,
        "seq_no": seq,
        "category": category,
        "text": text,
        "raw_type": raw_type or category.lower(),
        "bbox": bbox or [50, 400, 550, 600],
        "page_width": 1000,
        "page_height": 1000,
        "raw": {},
    }


def _records(ocr_text, heading="Ａ受注管理 －（３）受注入力"):
    picture = _record("pic", "Picture", 1, 3, raw_type="picture", bbox=BANNER_BBOX)
    ocr = _record("ocr", "Picture", 1, 4, ocr_text, raw_type="picture_ocr_text", bbox=BANNER_BBOX)
    section = _record("sec", "Section-header", 1, 1, heading)
    body = _record("body", "Text", 1, 2, "①伝票を選びます。②保存ボタンを押します。")
    return picture, [section, body, picture, ocr]


def test_banner_with_heading_repeat_and_copyright_is_decorative():
    picture, records = _records("OCR抽出テキスト:\nＡ受注管理－（３）受注入力\nCopyright© サンプル株式会社 All rights reserved.")
    role = classify_picture_record(picture, records)
    assert (role.role, role.reason, role.skip_vlm) == ("decorative", "section_banner_ocr", True)


def test_banner_with_only_copyright_is_decorative_and_heading_on_other_page_still_matches():
    picture, records = _records("OCR抽出テキスト:\nCopyright© サンプル株式会社 All rights reserved.")
    assert classify_picture_record(picture, records).reason == "section_banner_ocr"
    # 見出し record が別ページにあっても、同じ文書の見出しなら再掲とみなす
    picture, records = _records("OCR抽出テキスト:\nＡ受注管理 －（３）受注入力")
    records[0]["page"] = 7
    assert classify_picture_record(picture, records).reason == "section_banner_ocr"


def test_picture_with_other_ocr_lines_or_without_ocr_stays_content():
    picture, records = _records("OCR抽出テキスト:\nＡ受注管理 －（３）受注入力\n登録ボタン\nCopyright© サンプル株式会社")
    assert classify_picture_record(picture, records).role == "content"
    picture, records = _records("")
    assert classify_picture_record(picture, records).role == "content"
    # 短い見出し（4 文字未満）は部分一致では再掲とみなさない
    picture, records = _records("OCR抽出テキスト:\n登録ボタン", heading="登録")
    assert classify_picture_record(picture, records).role == "content"
