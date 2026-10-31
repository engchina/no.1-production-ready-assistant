"""#370: 同じページでも項目・処理区分の適用条件を保持する。"""


def test_excerpt_keeps_mode_name_attached_to_its_conditions_under_budget():
    from rag_engine.retrieval.evidence_selection import evidence_excerpt

    text = (
        "検索語: 取引先コード変更 統合元 統合先\n"
        * 60
        + "取引先コード統合\n・統合元は登録済みの取引先コードを指定します。\n・統合先は既存コードの選択または新規入力（半角8桁）です。\n"
        "重複取引先名寄せ\n・統合元と統合先とも登録済みの取引先コードを指定します。\n"
        "２. 実行ボタンで統合を確定します。\n"
    )
    excerpt = evidence_excerpt("取引先コードが変わったときの操作方法は？", text, 260)
    assert "重複取引先名寄せ\n・統合元と統合先とも登録済み" in excerpt
    assert "取引先コード統合\n・統合元は登録済み" in excerpt
    assert "半角8桁" in excerpt and "実行ボタンで統合を確定" in excerpt
    assert len(excerpt) <= 260
