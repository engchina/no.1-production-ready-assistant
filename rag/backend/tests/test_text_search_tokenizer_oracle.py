"""全文検索の分割(#588)の実 Oracle 26ai での評価。

本番と同じ lexer / stoplist(``RAG_TEXT_WORLD_LEXER`` / ``RAG_TEXT_STOPLIST``)の CONTEXT 索引を
一時表に作り、代表的な質問(送り仮名・活用・辞書に無い固有名詞・型番・英数字・ドメインキーワード)で
``_oracle_text_query`` の query が期待する文書を上位に出すことを確かめる。一時表は最後に消す。
未到達なら ``oracle_db`` fixture が skip する。

#588 の採用時の比較(同じ質問・文書。Hit@1 / Hit@3 / MRR): 標準(文字種の区切り)19/24/0.840、
Sudachi(OR で結ぶ)16/25/0.784、統合 25/26/0.981、統合で Sudachi なし 22/25/0.910(26 問)。
"""

from uuid import uuid4

import pytest

from app.clients.oracle import (
    _oracle_text_query,
    _text_search_index_text,
    oracle_text_index_parameters_sql,
)
from app.config import Settings
from tests import _oracle_test_db

DOMAIN_KEYWORDS = ["伝票区分", "楽々精算", "出荷基準年月"]
DOCUMENTS = {
    "d01": (
        "見積もり書の有効期限は発行日から30日です。期限を過ぎた見積もりは再発行を依頼してください。"
    ),
    "d02": (
        "パスワードの有効期限は90日です。"
        "期限が近づくとログイン時に変更を促すメッセージが表示されます。"
    ),
    "d03": "経費精算の締め切りは毎月25日です。締め切り後の申請は翌月扱いになります。",
    "d04": "経費精算システムへのログインは社員番号と初期パスワードで行います。",
    "d05": "出張申請は出発の1週間前までにワークフローから提出してください。上長の承認が必要です。",
    "d06": "休暇申請は前日までに勤怠システムから申請してください。",
    "d07": (
        "楽々精算でログイン障害が起きた場合は、ブラウザの"
        "キャッシュを削除してから再度ログインしてください。"
    ),
    "d08": "交通費の精算は月末にまとめて申請します。定期区間の運賃は精算できません。",
    "d09": "複合機 ABC-1234 のトナーを交換するには、前面カバーを開けてカートリッジを引き出します。",
    "d10": "複合機 ABC-5678 のトナー交換は、右側面のカバーを開けて行います。",
    "d11": "VPN の接続が頻繁に切断される場合は、クライアントを最新版へ更新してください。",
    "d12": "Wi-Fi に接続できないときは、無線 LAN のアダプターを再起動してください。",
    "d13": (
        "パスワードをリセットするには、ログイン画面の「"
        "パスワードを忘れた場合」から本人確認を行います。"
    ),
    "d14": "To reset your password, open the sign-in page and choose Forgot password.",
    "d15": "基準月修正画面では、出荷の基準月を変更できます。変更後は再集計が必要です。",
    "d16": "出荷指示書は倉庫システムから印刷します。",
    "d17": "伝票区分の変更は、伝票明細画面の区分欄から行います。確定済みの伝票は変更できません。",
    "d18": "伝票の印刷は、伝票一覧で対象を選んで印刷ボタンを押します。",
    "d19": "区分マスタには、取引先区分と商品区分を登録します。",
    "d20": "各種申請書のひな形は、共有フォルダの「様式集」に置いています。",
    "d21": "申請書の提出先は所属部門の総務担当です。",
    "d22": "社内システムに関するお問い合わせ窓口は情報システム部のヘルプデスクです。",
    "d23": "注文の取消は、出荷前であれば注文履歴画面から行えます。",
    "d24": "注文の確定後は、納期回答を待ってください。",
    "d25": "サーバーを再起動する手順は、運用手順書の第3章を参照してください。",
    "d26": "サーバーの監視アラートは運用チームへ通知されます。",
    "d27": "36協定で定める時間外労働の上限は、原則として月45時間です。",
    "d28": "給与の振込日は毎月25日です。休日の場合は前営業日になります。",
    "d29": "賞与の支給日は6月と12月の10日です。",
    "d30": "新入社員の PC アカウント発行には、所属長の承認が必要です。情報システム部が発行します。",
    "d31": "退職者の PC は、最終出社日に情報システム部へ返却してください。",
    "d32": "会議室の予約は、グループウェアの施設予約から行います。",
    "d33": "名刺の発注は、総務部へ申し込んでください。",
    "d34": "在宅勤務の申請は、前週の金曜日までに提出します。",
    "d35": "請求書の受領後、支払処理は経理部が行います。",
    "d36": "個人情報を含むファイルをメールで送る場合は、パスワードを設定して暗号化してください。",
    "d37": "見積依頼は購買システムから登録します。",
    "d38": "プリンター X200 の初期設定は、管理画面の設定メニューから行います。",
    "d39": "プリンター X300 の初期設定は、本体の操作パネルから行います。",
    "d40": "振込先口座の変更は、人事システムの口座情報から申請します。",
    "d41": "月次報告の締切は翌月5日です。",
    "d42": "楽々精算の申請期限は、利用した月の翌月10日です。",
    "d43": "経費精算の申請期限は翌月15日です。スマートフォンからでも楽々に申請できます。",
}
# (質問, 期待する文書, 検索・回答プロファイルのドメインキーワードを使うか)
CASES = [
    ("見積りの有効期限はいつまでですか", "d01", False),  # 送り仮名
    ("経費精算の締切日はいつですか", "d03", False),  # 送り仮名
    ("出張を申請したい", "d05", False),  # 活用
    ("注文を取り消したい", "d23", False),  # 活用・送り仮名
    ("給与はいつ振り込まれますか", "d28", False),  # 活用・送り仮名
    ("楽々精算にログインできない", "d07", False),  # 固有名詞(辞書に無い語)
    ("楽々精算のログイン障害", "d07", True),  # 固有名詞+ドメインキーワード
    ("ABC-1234のトナー交換手順を教えて", "d09", False),  # 型番
    ("VPN接続が切れる", "d11", False),  # 英数字
    ("パスワードリセットの方法", "d13", False),  # カタカナ複合語
    ("How do I reset my password?", "d14", False),  # 英語
    ("出荷基準年月を修正したい", "d15", False),  # 漢字複合語
    ("出荷基準年月を修正したい", "d15", True),  # 漢字複合語+ドメインキーワード
    ("伝票区分を変更する方法", "d17", True),  # ドメインキーワード
    ("申請書のひな形はどこにありますか", "d20", False),  # ひらがなを含む語
    ("問合せ窓口はどこですか", "d22", False),  # 送り仮名
    ("サーバの再起動手順", "d25", False),  # 表記ゆれ(長音)
    ("36協定の上限時間は", "d27", False),  # 数字+漢字
    (
        "新しく入社した社員のPCアカウントの発行手続きと必要な承認者を教えてください",
        "d30",
        False,
    ),  # 長い質問
    ("個人情報をメールで送るときの注意点", "d36", False),  # 一般語
    ("在宅勤務はいつまでに申請すればよいですか", "d34", False),  # 一般語
    ("見積もりを依頼したい", "d37", False),  # 送り仮名
    ("X200の初期設定", "d38", False),  # 型番
    ("振り込み先の口座を変更したい", "d40", False),  # 送り仮名
    ("月次報告の締め切りはいつ", "d41", False),  # 送り仮名
    ("楽々精算の申請期限", "d42", False),  # 固有名詞(辞書に無い語)
]


def _rank(hits: list[tuple[str, float]], expected: str) -> float | None:
    """期待する文書の順位(同点は平均順位)。当たらなければ None。"""
    scores = dict(hits)
    if expected not in scores:
        return None
    score = scores[expected]
    higher = sum(1 for _doc, value in hits if value > score)
    ties = sum(1 for doc, value in hits if value == score and doc != expected)
    return 1 + higher + ties / 2


@pytest.mark.usefixtures("oracle_db")
def test_text_search_query_ranks_expected_documents_on_real_oracle() -> None:
    table = f"RAG_TMP_TOKENIZER_{uuid4().hex[:8].upper()}"
    connection = _oracle_test_db._connect()
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"CREATE TABLE {table} (doc_id VARCHAR2(16) PRIMARY KEY, body VARCHAR2(4000))"
            )
            cursor.executemany(
                f"INSERT INTO {table} (doc_id, body) VALUES (:1, :2)", list(DOCUMENTS.items())
            )
            connection.commit()
            cursor.execute(
                f"CREATE INDEX {table}_TX ON {table} (body) INDEXTYPE IS CTXSYS.CONTEXT "
                + oracle_text_index_parameters_sql()
            )
            ranks: dict[tuple[str, bool], float | None] = {}
            for question, expected, use_keywords in CASES:
                settings = Settings(rag_domain_keywords=DOMAIN_KEYWORDS if use_keywords else [])
                query = _oracle_text_query(question, settings=settings)
                assert query is not None, question
                cursor.execute(
                    f"SELECT doc_id, SCORE(1) FROM {table} WHERE CONTAINS(body, :q, 1) > 0",
                    {"q": query},
                )
                hits = [(str(row[0]), float(row[1])) for row in cursor.fetchall()]
                ranks[(question, use_keywords)] = _rank(hits, expected)
        finally:
            cursor.execute(f"DROP TABLE {table} PURGE")
    finally:
        connection.close()

    misses = [key for key, rank in ranks.items() if rank is None or rank > 3]
    assert misses == []  # すべて 3 位以内
    top1 = sum(1 for rank in ranks.values() if rank is not None and rank <= 1)
    mrr = sum(1 / rank for rank in ranks.values() if rank) / len(ranks)
    # 「見積もりを依頼したい」は、見積もり・依頼の両方を含む別の文書が 1 位になる(既知の制約)。
    assert top1 >= len(CASES) - 1
    assert mrr >= 0.95


# 互換文字を含む本文（#1336）。WORLD_LEXER は全角 / 半角の違いは同一視するが、
# 互換文字は同一視しない。
# 質問の側は NFKC でこれらを 1・V・(株) などにするため、索引の側も同じ形にして初めて当たる。
COMPATIBILITY_DOCUMENTS = {
    "c01": "手順①で申請書を作成し、手順②で上長に提出します。",
    "c02": "第Ⅴ章の定例処理では、月次の締めを行います。",
    "c03": "㈱サンプル商事との契約は、法務部が確認します。",
    "c04": "荷物の重さの上限は５㌔です。",
    "c05": "サーバー室の温度は２５℃以下に保ちます。",
    "c06": "エラーＥ１０２３が出たら認証ログを確かめます。",
}
COMPATIBILITY_CASES = [
    ("手順1で何をしますか", "c01"),
    ("第V章の定例処理", "c02"),
    ("(株)サンプル商事との契約", "c03"),
    ("荷物の重さの上限は何キロ", "c04"),
    ("サーバー室の温度は何°C以下", "c05"),
    ("E1023のエラー", "c06"),
]


@pytest.mark.usefixtures("oracle_db")
def test_compatibility_characters_hit_when_index_text_is_normalized_on_real_oracle() -> None:
    """索引する文字列を正規化（``_text_search_index_text``）すると互換文字にも当たる（#1336）。"""
    table = f"RAG_TMP_TOKENIZER_{uuid4().hex[:8].upper()}"
    connection = _oracle_test_db._connect()
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"CREATE TABLE {table} (doc_id VARCHAR2(16) PRIMARY KEY, body VARCHAR2(4000))"
            )
            cursor.executemany(
                f"INSERT INTO {table} (doc_id, body) VALUES (:1, :2)",
                [
                    (doc_id, _text_search_index_text(body))
                    for doc_id, body in COMPATIBILITY_DOCUMENTS.items()
                ],
            )
            connection.commit()
            cursor.execute(
                f"CREATE INDEX {table}_TX ON {table} (body) INDEXTYPE IS CTXSYS.CONTEXT "
                + oracle_text_index_parameters_sql()
            )
            misses: list[str] = []
            for question, expected in COMPATIBILITY_CASES:
                query = _oracle_text_query(question, settings=Settings(rag_domain_keywords=[]))
                assert query is not None, question
                cursor.execute(
                    f"SELECT doc_id, SCORE(1) FROM {table} WHERE CONTAINS(body, :q, 1) > 0",
                    {"q": query},
                )
                hits = [(str(row[0]), float(row[1])) for row in cursor.fetchall()]
                rank = _rank(hits, expected)
                if rank is None or rank > 1:
                    misses.append(question)
        finally:
            cursor.execute(f"DROP TABLE {table} PURGE")
    finally:
        connection.close()

    assert misses == []


@pytest.mark.usefixtures("oracle_db")
@pytest.mark.parametrize("overflow", ["tokens", "characters"])
def test_overflow_preserves_explicit_conditions_in_executed_oracle_query(overflow: str) -> None:
    """語数・文字数で省略しても、実際の CONTAINS の bind に保護条件を残す。"""
    from rag_engine.retrieval.text_search_tokenizer import (
        MAX_ORACLE_TEXT_QUERY_CHARS,
        tokenize_text_search_query_with_trace,
    )

    keywords = [f"候補語{letter}" for letter in "abcdefghijklmnopqrstuvxyz"]
    prefix = (
        " ".join(keywords) if overflow == "tokens" else "x" * (MAX_ORACLE_TEXT_QUERY_CHARS + 100)
    )
    question = prefix + " ID-789 v2.3 2026/10/27 50以上"
    settings = Settings(rag_domain_keywords=keywords)
    tokenization = tokenize_text_search_query_with_trace(question, domain_keywords=keywords)
    query = _oracle_text_query(question, settings=settings)
    assert query == tokenization.oracle_text_query
    assert query is not None
    assert len(query) <= MAX_ORACLE_TEXT_QUERY_CHARS
    assert {"id-789", "v2.3", "2026/10/27", "50"} <= set(tokenization.tokens)
    assert tokenization.truncated
    if overflow == "characters":
        assert tokenization.character_limit_tokens
    table = f"RAG_TMP_OVERFLOW_{uuid4().hex[:8].upper()}"
    connection = _oracle_test_db._connect()
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"CREATE TABLE {table} (doc_id VARCHAR2(16) PRIMARY KEY, body VARCHAR2(4000))"
        )
        cursor.executemany(
            f"INSERT INTO {table} (doc_id, body) VALUES (:1, :2)",
            [
                (
                    "target",
                    _text_search_index_text(
                        "対象 ID-789、版 v2.3、日付 2026/10/27、数値条件 50 の合成資料。"
                    ),
                ),
                (
                    "other",
                    _text_search_index_text(
                        "対象 ID-999、版 v9.9、日付 2025/01/01、数値条件 5 の合成資料。"
                    ),
                ),
            ],
        )
        connection.commit()
        cursor.execute(
            f"CREATE INDEX {table}_TX ON {table} (body) INDEXTYPE IS CTXSYS.CONTEXT "
            + oracle_text_index_parameters_sql()
        )
        cursor.execute(f"SELECT doc_id FROM {table} WHERE CONTAINS(body, :q, 1) > 0", {"q": query})
        assert "target" in {str(row[0]) for row in cursor.fetchall()}
    finally:
        cursor.execute(f"DROP TABLE {table} PURGE")
        connection.close()
