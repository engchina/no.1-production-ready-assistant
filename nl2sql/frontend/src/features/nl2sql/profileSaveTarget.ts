/** 新規作成の編集対象（URL の `?profile=new`）。 */
export const NEW_PROFILE_TARGET = "new";

/**
 * 保存の後に届いた応答（Oracle 反映のジョブの開始など）を、今の画面に反映してよいか（#1784）。
 *
 * 保存を始めたときの編集対象（`submittedTarget`）を今も開いていれば反映する。新規作成の保存は、
 * 作成の直後に URL を作成した ID（`?profile=<id>`）へ切り替えるので、作成した業務プロファイル
 * （`savedProfileId`）を開いている間も同じ保存の続きとして反映する。別の業務プロファイル・一覧へ
 * 移ったときは反映しない。
 */
export function isProfileSaveTargetCurrent(
  currentTarget: string | null,
  submittedTarget: string | null,
  savedProfileId: string
): boolean {
  if (currentTarget === submittedTarget) return true;
  return (
    submittedTarget === NEW_PROFILE_TARGET &&
    Boolean(savedProfileId) &&
    currentTarget === savedProfileId
  );
}
