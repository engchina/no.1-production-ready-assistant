"""「AI 活用」の下の機能のデータの見え方（3 製品共通。#1889）。

規則: **system_admin は全員のデータ、それ以外の利用者は自分のデータだけ**を、一覧・詳細・操作で
扱う（RAG のチャット・RAG 検索・実行履歴、NL2SQL のチャット・SQL 生成・SQL 直接実行・SQL から質問・
実行履歴、Agent のチャット・承認・実行履歴）。

- system_admin は SYSTEM_ADMIN のロールを持つ利用者（構成管理者 `system_admin` と、local の
  ローカル利用者を含む）。製品の管理権限（`agent.admin` など）やメニュー権限は、全員を見る
  理由にしない。
- 判定は backend で行い、一覧は SQL / store の条件で絞る（画面で隠すだけにしない）。他人のデータの
  詳細・操作は 404 にして存在を漏らさない（製品の API の契約。`OWNER_NOT_FOUND_STATUS`）。
- system_admin が他の利用者のデータにできるのは、閲覧（一覧・詳細・処理の経過・実行履歴）と
  運用の操作（停止・取り消し・削除、Agent の承認待ちの判断と再開。続きは Run を実行した人の
  権限で動く）だけ（#3244。#1928 を置き換え）。新しい実行・内容を足す操作（続きの質問・SQL の
  実行・再実行・確認や明確化への回答・評価・会話の名前の変更）は持ち主だけで、system_admin でも
  403（`OTHERS_CONTENT_FORBIDDEN_STATUS`。MCP のツールのエラーの code は
  `OTHERS_CONTENT_FORBIDDEN_CODE`）。system_admin の権限・対象範囲で作った内容が持ち主の
  会話に入り、持ち主が権限の外の資料・SQL・ツールの出力を見るのを防ぐ。調べ直すときは
  system_admin が自分の会話で再現する。運用の操作は操作した人（actor）を残し、持ち主は変えない。
- 実行（RAG・NL2SQL の実行履歴の行、Agent の Run）は、実行した人（executor）と会話の持ち主の
  両方に見える（system_admin は全員。#1954）。system_admin が他の利用者の会話で実行した往復は、
  持ち主の実行履歴にも出す。利用量・API キーの上限・監査は実行した人に数える。一覧は
  「すべて / 自分が実行 / 他の人が実行」（`RUN_EXECUTOR_FILTERS`。既定 `all`）で絞れる。
- system_admin の一覧には「利用者」の列（共通の表示名。`AuthService.display_identities`。
  #1696）を出す。
- 「AI 活用」の外（改善・運用の監査・利用状況・フィードバック管理など）はこの規則の対象外で、
  それぞれの権限のまま。
"""

from __future__ import annotations

from typing import Literal, Protocol

# 他人のデータの詳細・操作に返す status（存在を漏らさない）。
OWNER_NOT_FOUND_STATUS = 404
# system_admin が他の利用者のデータに新しい実行・内容を足そうとしたときの status と文言（#3244）。
# 見えるデータなので 404 にはしない。
OTHERS_CONTENT_FORBIDDEN_STATUS = 403
OTHERS_CONTENT_FORBIDDEN_MESSAGE = (
    "他の利用者の会話・実行には、続きの質問・実行・回答・評価を追加できません。"
    "閲覧・停止・削除はできます。調べ直すときは自分の会話で質問してください。"
)
# 外部の MCP（API キー・サービストークン）のツールのエラーの code（3 製品で同じ。#3244）。
OTHERS_CONTENT_FORBIDDEN_CODE = "OTHERS_CONTENT_FORBIDDEN"

# 実行履歴の「実行者」の絞り込み（#1954）: すべて / 自分が実行 / 他の人が実行。既定は all。
RunExecutorFilter = Literal["all", "mine", "others"]
RUN_EXECUTOR_FILTERS: tuple[RunExecutorFilter, ...] = ("all", "mine", "others")
DEFAULT_RUN_EXECUTOR_FILTER: RunExecutorFilter = "all"


class _AdminFlag(Protocol):
    @property
    def is_system_admin(self) -> bool: ...


class _UserPrincipal(_AdminFlag, Protocol):
    @property
    def user_uuid(self) -> str: ...


def can_view_all_users_data(principal: _AdminFlag | None) -> bool:
    """「AI 活用」の下の機能で、全員のデータを見られるか（system_admin だけ）。"""
    return bool(principal is not None and principal.is_system_admin)


def owner_filter_user_uuid(principal: _UserPrincipal) -> str | None:
    """一覧を絞る持ち主の UUID。全員を見られる利用者は None（絞らない）。"""
    return None if can_view_all_users_data(principal) else principal.user_uuid


def can_access_user_data(principal: _UserPrincipal | None, owner_user_uuid: str | None) -> bool:
    """`owner_user_uuid` の持ち主のデータの詳細を見て操作してよいか。

    system_admin は他の利用者のデータも見て、運用の操作（停止・取り消し・削除・承認の判断）が
    できる。新しい実行・内容を足す操作は、さらに `can_add_user_content` で持ち主だけに絞る
    （#3244）。

    持ち主の分からないデータ（記録の無い古い行など）は system_admin だけが扱える。
    """
    if principal is None:
        return False
    if can_view_all_users_data(principal):
        return True
    owner = (owner_user_uuid or "").strip()
    return bool(owner) and owner == principal.user_uuid


def can_add_user_content(principal: _UserPrincipal | None, owner_user_uuid: str | None) -> bool:
    """`owner_user_uuid` の持ち主のデータに、新しい実行・内容を足してよいか（持ち主だけ。#3244）。

    続きの質問・SQL の実行・再実行・確認や明確化への回答・評価・会話の名前の変更が対象。
    system_admin も他の利用者のデータには足せない（閲覧と運用の操作は `can_access_user_data`）。
    持ち主の分からないデータには誰も足せない。
    """
    if principal is None:
        return False
    owner = (owner_user_uuid or "").strip()
    return bool(owner) and owner == principal.user_uuid


def can_access_run(
    principal: _UserPrincipal | None,
    *,
    executor_user_uuid: str | None,
    owner_user_uuid: str | None,
) -> bool:
    """実行（実行履歴の行・Run）の詳細を見てよいか（#1954）。

    実行した人（executor）・会話の持ち主・system_admin。会話の無い実行は `owner_user_uuid` に
    executor を渡す（または None）。一覧の SQL / store の条件も同じ範囲にする。
    """
    if principal is None:
        return False
    return can_access_user_data(principal, executor_user_uuid) or (
        owner_user_uuid is not None and can_access_user_data(principal, owner_user_uuid)
    )


def run_executor_matches(
    principal: _UserPrincipal,
    executor_user_uuid: str | None,
    executor_filter: RunExecutorFilter,
) -> bool:
    """実行者の絞り込み（`RUN_EXECUTOR_FILTERS`）に、この実行が入るか（#1954）。

    `mine` は自分が実行した物、`others` は自分以外（持ち主の会話で system_admin が実行した物など）。
    SQL で絞る製品は同じ条件を SQL に書く（`executor = :me` /
    `executor <> :me OR executor IS NULL`）。
    """
    if executor_filter == "all":
        return True
    mine = (executor_user_uuid or "").strip() == principal.user_uuid
    return mine if executor_filter == "mine" else not mine
