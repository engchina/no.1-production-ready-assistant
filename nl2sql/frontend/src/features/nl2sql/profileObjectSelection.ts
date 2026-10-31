/** 業務プロファイルの許可オブジェクト選択に関する純粋ロジック。 */

import { formatDbObjectPart, normalizeDbObjectKey, splitDbObjectName } from "./dbObjectIdentity";

/**
 * 表記の揺れを吸収した突合キー（canonical な `OWNER.OBJECT`）。
 *
 * 引用されていない部分は大文字、`"..."` で引用された部分は大文字小文字を保つ
 * （`normalizeDbObjectKey`）。`SALES."Mixed_Case"` を大文字の同名表と同じキーにしない（#561）。
 */
export function normalizeObjectKey(name: string) {
  return normalizeDbObjectKey(name);
}

/**
 * `OWNER.` 形式の接頭辞（または owner 名）を owner の突合キーにする。
 * owner はスキーマ一覧のカタログ上の名前（大文字小文字を保持）。引用なしの入力として大文字化すると
 * `"Sales"` のグループで `SALES` の object を数える・外す（#563）。
 */
function normalizeOwnerKey(ownerPrefix: string) {
  return formatDbObjectPart(ownerPrefix.trim().replace(/\.$/u, ""));
}

/** object 名が指定 owner に属するか。文字列の前方一致ではなく owner 部分で比較する。 */
export function isObjectInOwner(name: string, owner: string) {
  return splitDbObjectName(name)?.owner === normalizeOwnerKey(owner);
}

/** 選択済み object のうち、指定 owner に属する件数。 */
export function countSelectedObjectsInOwner(selected: Iterable<string>, owner: string) {
  let count = 0;
  for (const name of selected) {
    if (isObjectInOwner(name, owner)) count += 1;
  }
  return count;
}

export interface SchemaBulkSelectionInput {
  /** 現在の選択(allowedTables / allowedViews)。 */
  current: readonly string[];
  /** サーバから取得した対象 object 名(正規化済み・OWNER.OBJECT)。 */
  snapshot: readonly string[];
  /** 対象スキーマの接頭辞(`OWNER.`)。 */
  ownerPrefix: string;
  /** true=全選択 / false=全解除。 */
  select: boolean;
  /**
   * 検索フィルタ適用中か。適用中は snapshot(=ヒットした object)だけを対象にし、
   * フィルタ外の選択済み object は保持する。
   */
  filtered: boolean;
}

/**
 * スキーマ単位の一括選択/解除を適用する。
 *
 * フィルタ適用中に「このスキーマを全選択/全解除」がスキーマ全体へ波及すると
 * 許可オブジェクト(=アクセススコープ)が意図せず広がるため、適用範囲を
 * snapshot に限定する。
 */
export function applySchemaBulkSelection({
  current,
  snapshot,
  ownerPrefix,
  select,
  filtered,
}: SchemaBulkSelectionInput): string[] {
  const scope = new Set(snapshot.map(normalizeObjectKey));
  const retained = current.filter((name) =>
    filtered ? !scope.has(normalizeObjectKey(name)) : !isObjectInOwner(name, ownerPrefix)
  );
  return select ? [...retained, ...snapshot] : retained;
}

/**
 * 1 object の選択をトグルする。
 *
 * 保存済みの値は引用符付き・小文字(`"APP"."ORDERS"` / `app.orders`)の
 * ことがある。チェック表示は正規化キーで判定しているため、トグル側も同じキーで
 * 突合しないと「チェックは付くのに外せず、重複が積まれる」状態になる。
 * 追加する値も正規化キーにする。引用名(`SALES."Mixed_Case"`)は大文字化しない。
 */
export function toggleObjectSelection(current: readonly string[], name: string): string[] {
  const key = normalizeObjectKey(name);
  const next = current.filter((item) => normalizeObjectKey(item) !== key);
  return next.length === current.length ? [...current, key] : next;
}

/** 選択済みの突合用集合(表示・件数・一括操作で共有する)。 */
export function selectedObjectKeys(selected: readonly string[]): Set<string> {
  return new Set(selected.map(normalizeObjectKey));
}

/** 未保存判定で比較する編集フォームの最小形。 */
export interface ProfileFormComparable {
  allowedTables: readonly string[];
  allowedViews: readonly string[];
}

function withOrderInsensitiveSelection<T extends ProfileFormComparable>(form: T) {
  return {
    ...form,
    allowedTables: [...form.allowedTables].map(normalizeObjectKey).sort(),
    allowedViews: [...form.allowedViews].map(normalizeObjectKey).sort(),
  };
}

/**
 * 編集フォームが読込時と同一かを判定する。
 *
 * 許可オブジェクトはチェックの付け外しで配列順が変わるため、集合として比較する。
 * 単純な JSON 比較のままだと「付けて外して元に戻した」だけで未保存扱いになる。
 */
export function profileFormEquals<T extends ProfileFormComparable>(left: T, right: T): boolean {
  return (
    JSON.stringify(withOrderInsensitiveSelection(left)) ===
    JSON.stringify(withOrderInsensitiveSelection(right))
  );
}
