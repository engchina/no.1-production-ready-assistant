/**
 * SQL 生成の画面のスキーマ参照の状態（#3413）。
 *
 * スキーマの一覧（`useSchemaObjects`）は使える業務プロファイルが決まるまで無効の query にする。TanStack Query の
 * 無効の query はデータが無い間 `isPending` のままなので、`isPending` だけで読み込み中にすると、業務プロファイルが
 * 未選択・使えない間ずっと読み込み中の表示（経過時間・Skeleton）が出る。何かを読み込んでいる間だけ `loading` にし、
 * 業務プロファイルが決まっていないときは `profile-unselected`（選ぶと表と列を出すと案内する）にする。
 */
export type SchemaReferenceState = "profile-unselected" | "loading" | "ready";

export function schemaReferenceState({
  profileId,
  profilesPending,
  prefillPending,
  profileReady,
  profileFailed,
  schemaPending,
}: {
  /** 選んでいる業務プロファイル（空は未選択）。 */
  profileId: string;
  /** 業務プロファイルの一覧の初回の読込中。 */
  profilesPending: boolean;
  /** 受け取った業務プロファイル（実行履歴の「SQL 生成で開く」）を一覧で確かめている途中。 */
  prefillPending: boolean;
  /** 選んだ業務プロファイルが使える（スキーマの一覧を取りに行ける）。 */
  profileReady: boolean;
  /** 選んだ業務プロファイルの利用の情報を取れなかった（削除済み・権限なし・読込の失敗）。 */
  profileFailed: boolean;
  /** スキーマの一覧がまだ無い（初回の読込中、または無効の query）。 */
  schemaPending: boolean;
}): SchemaReferenceState {
  if (!profileId) return profilesPending || prefillPending ? "loading" : "profile-unselected";
  if (!profileReady) return profileFailed ? "profile-unselected" : "loading";
  return schemaPending ? "loading" : "ready";
}
