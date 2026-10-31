import type { RequestOptions, SecurityRole } from "../users-roles/types";
import type { RolePermissionTargetMessages } from "./messages";

/** `GET /api/security/permissions` の 1 件（3製品で同じ形。#220）。 */
export interface PermissionDefinition {
  code: string;
  group: string;
  label: string;
  description: string;
  /** この権限を付けると自動的に付く権限コード。 */
  implies: string[];
}

/** 権限を持つロール。製品は利用できる対象の ID などを足した型をそのまま渡してよい。 */
export interface PermissionRole extends SecurityRole {
  permissions: string[];
}

/** 利用できる対象の候補 1 件（業務プロファイル・検索・回答プロファイル・ナレッジベース・エージェントなど）。 */
export interface RolePermissionTargetItem {
  id: string;
  name: string;
  description?: string;
  /** 名前の後ろに括弧で添える補足（例: カテゴリ）。 */
  secondary?: string;
  /** 名前の横に出す状態（例: アーカイブ済み）。 */
  status?: string;
}

/**
 * 候補の一覧にない ID を直接入力して追加する欄の設定（#215）。
 * マスタを持たない対象（例: Agent の検索・回答プロファイル）に使う。入力した ID は候補に足して選択状態にする。
 */
export interface RolePermissionCustomIdOptions {
  /** 入力欄のラベル（例: 検索・回答プロファイル ID を直接入力）。 */
  label: string;
  placeholder?: string;
  /** 入力欄の補足。 */
  hint?: string;
  /** 追加ボタンの文言。 */
  addLabel: string;
  /** 受け付ける ID の形式（前後の空白を除いた値で判定する）。無ければ空でない文字列を受け付ける。 */
  pattern?: RegExp;
  /** 空・形式に合わない ID を追加しようとしたときの文言。 */
  invalidMessage: string;
  /** 候補の一覧にない ID（直接入力・保存済み）に添える状態（例: 直接入力）。 */
  customStatus?: string;
}

/**
 * 候補の問い合わせ（#608）。候補は数千件になりうるので、サーバー側で検索とページングをする。
 * 3 製品の backend は `q` / `limit` / `offset` / `ids`（繰り返しのクエリ）を受け、`Page`（items / total）を返す。
 */
export interface RolePermissionTargetQuery {
  /** 検索語（名前・説明などの部分一致。空なら全件）。 */
  q: string;
  limit: number;
  offset: number;
  /** その ID だけを読む（ロールに選択済みの対象の名前を解決する）。検索語は空で渡す。 */
  ids?: string[];
}

/**
 * 候補の 1 ページ。`warning` は候補の一部だけを読めたとき（#240）の理由（利用者向けの文言そのまま）で、
 * 読めた候補を表示したまま画面上部の warning の Banner に出す。
 */
export interface RolePermissionTargetPage {
  items: RolePermissionTargetItem[];
  /** 条件に一致する全件数。 */
  total: number;
  warning?: string;
}

/**
 * ロールに付ける「利用できる対象」の 1 種類。製品固有の対象はこの形で権限管理画面へ差し込む。
 * 例: NL2SQL の業務プロファイル、RAG の検索・回答プロファイル・ナレッジベース、Agent のエージェント・検索・回答プロファイル。
 */
export interface RolePermissionTargetSection<R extends PermissionRole = PermissionRole> {
  /** draft.targets の key。テスト ID と要素 ID の `security-roles-<key>-*` にも使う。 */
  key: string;
  messages: RolePermissionTargetMessages;
  /**
   * 候補を検索・ページングで読む（#608）。編集画面の候補は `q`（検索欄）・`limit` / `offset`（「さらに読み込む」）で、
   * ロールに選択済みの対象の名前は `ids` で読む。失敗しても画面は警告を出してロール一覧を表示し続ける。
   */
  query: (
    query: RolePermissionTargetQuery,
    options: RequestOptions & { signal: AbortSignal },
  ) => Promise<RolePermissionTargetPage>;
  /** ロールに保存済みの対象 ID。 */
  selectedIds: (role: R) => string[];
  /**
   * 有効な権限コード（implies 展開後）から「全件が対象」かを判定する（例: 管理権限を持つ）。
   * SYSTEM_ADMIN は常に全件が対象。全件のときは個別選択を出さず、保存では空の一覧を送る。
   */
  grantsAll?: (effectivePermissions: ReadonlySet<string>) => boolean;
  /**
   * 候補にない ID の直接入力を許可する（任意）。指定すると、選択済みで候補にない ID も
   * 一覧・詳細に ID のまま表示する。
   */
  allowCustomIds?: RolePermissionCustomIdOptions;
}

/** 保存する内容。targets は対象の key ごとの ID 一覧（全件が対象のときは空）。 */
export interface RolePermissionsDraft {
  permissions: string[];
  targets: Record<string, string[]>;
}

export interface RolePermissionsApi<R extends PermissionRole = PermissionRole> {
  /** アーカイブ済みを含むロール一覧（`GET /api/security/roles?include_archived=true`）。 */
  roles: (includeArchived: boolean, options?: RequestOptions) => Promise<R[]>;
  /** 権限カタログ（`GET /api/security/permissions`）。 */
  permissions: (options?: RequestOptions) => Promise<PermissionDefinition[]>;
  /** 製品ごとの保存 API（例: NL2SQL `PUT /api/security/roles/{id}/permissions`）。保存後のロールを返す。 */
  save: (role: R, draft: RolePermissionsDraft) => Promise<R>;
}
