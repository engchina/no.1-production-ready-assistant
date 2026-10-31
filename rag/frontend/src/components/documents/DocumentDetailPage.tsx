import { PageBody, PageHeader, Skeleton } from "@production-ready/ui";
import { useNavigate, useSearchParams } from "react-router-dom";

import { EditorTargetState } from "@/components/layout/EntityLayout";
import { StatusBadge } from "@/components/StatusBadge";
import { t } from "@/lib/i18n";
import { recipeQueueWait } from "@/lib/ingestion-queue";
import { confirmPendingLeave } from "@/lib/leave-guard";
import { useDocument, useDocumentRecipes } from "@/lib/queries";
import { APP_ROUTES } from "@/lib/routes";
import { DocumentWorkspace } from "./DocumentWorkspace";
import { documentDisplayStatus, selectDocumentRecipe } from "./DocumentWorkspace.logic";

/**
 * 文書詳細（`/documents/:id`）。見出しはナレッジベース・検索・回答プロファイルの詳細と同じ構成にする（#581）:
 * `PageHeader`（パンくず「文書インデックス › ファイル名」・状態のバッジ・「一覧へ戻る」）→ `PageBody` の本文
 * （`DocumentWorkspace`。処理レシピ・処理の開始 / 承認 / 再試行・プレビューなどは本文のまま）。
 *
 * - 読み込み中・見つからない・取得の失敗は、KB・検索・回答プロファイルと共有の `EditorTargetState` で、同じ見出しを
 *   先に出して本文だけを切り替える。取得済みの文書があれば、ポーリング中の一時的な失敗で本文を置き換えない
 *   （本文の `DocumentWorkspace` と同じ。#281）。
 * - 状態は本文と同じく、URL の `?recipe=` で選んだレシピの状態（無ければ文書の状態）。工程の job が
 *   順番待ちなら「〈工程〉の順番待ち」（#2026）。
 * - 「一覧へ戻る」は `navigate()` で移るため、抽出確認の未保存の編集があれば先に同じ確認を通す
 *   （パンくずのリンクは共有の離脱ガードが確認する）。
 */
export function DocumentDetailPage({ documentId }: { documentId: string }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const query = useDocument(documentId);
  const recipesQuery = useDocumentRecipes(documentId);
  const listLabel = t("nav.fileList");
  const backToList = async () => {
    if (await confirmPendingLeave()) navigate(APP_ROUTES.fileList);
  };

  if (!query.data) {
    return (
      <EditorTargetState
        id={documentId}
        listLabel={listLabel}
        error={query.isError ? query.error : null}
        loadingLabel={t("documents.detail.loading")}
        loadingTestId="document-detail-loading"
        errorFallback={t("workspace.notFound")}
        skeleton={<Skeleton className="h-80 w-full rounded-lg" />}
        onBack={() => void backToList()}
        onRetry={() => void query.refetch()}
      />
    );
  }

  const doc = query.data;
  const selectedRecipe = selectDocumentRecipe(recipesQuery.data, searchParams.get("recipe"));
  const status = documentDisplayStatus(selectedRecipe, doc.status);
  // 次の工程の job が worker の空きを待っている間は「確認待ち」でなく「〈工程〉の順番待ち」（#2026）。
  const queuedPhase = recipeQueueWait(selectedRecipe)?.phase ?? null;

  return (
    <div>
      <PageHeader
        wide
        title={doc.file_name}
        status={<StatusBadge status={status} queuedPhase={queuedPhase} />}
        // 一覧へ戻るは左上（#618）。
        back={{
          label: t("common.backToList"),
          ariaLabel: t("editor.backToListOf", { list: listLabel }),
          onClick: () => void backToList(),
          testId: "editor-back",
        }}
      />
      <PageBody wide>
        <DocumentWorkspace documentId={documentId} showTitle={false} />
      </PageBody>
    </div>
  );
}
