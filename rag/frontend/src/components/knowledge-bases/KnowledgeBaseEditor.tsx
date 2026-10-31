import {
  Banner,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  FormActionBar,
  FormGrid,
  FormStatus,
  ObjectActionBar,
  PageBody,
  PageHeader,
  SaveErrorBanner,
  StatusBadge,
  TextField,
} from "@production-ready/ui";
import { Database, Library, RotateCcw, Save } from "lucide-react";
import { useRef, useState, type FormEvent, type ReactNode } from "react";

import { useEntityEditorDraft } from "@/components/layout/use-entity-editor-draft";
import {
  ApiError,
  DEFAULT_KNOWLEDGE_BASE_NAME,
  type KnowledgeBaseDetail,
} from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import { confirmPendingLeave } from "@/lib/leave-guard";
import { t } from "@/lib/i18n";
import { useCreateKnowledgeBase, useUpdateKnowledgeBase } from "@/lib/queries";
import { firstInvalidFieldId, focusFirstInvalidField } from "@/lib/required-fields";
import { toast } from "@/lib/toast";
import { KnowledgeBaseStatusPill } from "./KnowledgeBaseStatusPill";
import {
  DESCRIPTION_MAX_LENGTH,
  NAME_MAX_LENGTH,
  useKnowledgeBaseActions,
  validateKnowledgeBaseDescription,
  validateKnowledgeBaseName,
} from "./knowledge-base-actions";

export interface KnowledgeBaseDraft {
  name: string;
  description: string;
}

export function isKnowledgeBaseDraft(value: unknown): value is KnowledgeBaseDraft {
  const draft = value as KnowledgeBaseDraft;
  return (
    typeof draft === "object" &&
    draft !== null &&
    typeof draft.name === "string" &&
    typeof draft.description === "string"
  );
}

function draftSignature(draft: KnowledgeBaseDraft) {
  return JSON.stringify({ name: draft.name.trim(), description: draft.description.trim() });
}

const NAME_FIELD_ID = "knowledge-base-name";
const DESCRIPTION_FIELD_ID = "knowledge-base-description";

/**
 * ナレッジベースの全画面エディタ（新規 / 詳細。#555）。検索・回答プロファイルのエディタと同じ構成にする
 * （platform UX 契約 page-archetypes.md §1 A、RAG の docs/frontend-page-archetypes-spec.md）。
 *
 * - `PageHeader`: パンくず（ナレッジベース › 対象名）・状態・件数、`一覧へ戻る`（secondary）と
 *   `保存する` / `作成する`（primary）。
 * - 本文: 「基本情報」のカード（名前・説明。見出しに対象の操作の `ObjectActionBar`）→ `children`
 *   （詳細だけ。所属文書・検索テスト・関係情報・構築フロー）。
 * - 未保存の名前・説明は下書きとしてこのタブに残し、離脱を確認する（検索・回答プロファイルと同じ部品）。
 */
export function KnowledgeBaseEditor({
  initial,
  onBack,
  onCreated,
  onArchived,
  children,
}: {
  /** 詳細（編集）のときのサーバーの値。無ければ新規作成。 */
  initial?: KnowledgeBaseDetail;
  onBack: () => void;
  /** 作成に成功したとき（作成した対象の詳細へ履歴を積まずに移る）。 */
  onCreated?: (id: string) => void;
  /** 詳細からアーカイブしたとき（一覧へ履歴を積まずに戻る）。 */
  onArchived?: () => void;
  children?: ReactNode;
}) {
  const create = useCreateKnowledgeBase();
  const update = useUpdateKnowledgeBase();
  const actionsFor = useKnowledgeBaseActions({ onArchived: () => onArchived?.() });
  const formRef = useRef<HTMLFormElement>(null);
  const editor = useEntityEditorDraft<KnowledgeBaseDraft>({
    field: "knowledgeBases.draft",
    scope: initial?.id ?? "new",
    initial: { name: initial?.name ?? "", description: initial?.description ?? "" },
    isDraft: isKnowledgeBaseDraft,
    signature: draftSignature,
    leaveDescription: t("knowledgeBases.leaveGuard.description"),
  });
  const { draft, setDraft, dirty } = editor;
  // 欄ごとに、フォーカスが外れたとき・送信したときから検証結果を出す（messaging.md §3.2）。
  const [touched, setTouched] = useState({ name: false, description: false });

  const isDefault = initial?.name === DEFAULT_KNOWLEDGE_BASE_NAME;
  const isArchived = initial?.status === "ARCHIVED";
  const pending = create.isPending || update.isPending;
  const mutation = initial ? update : create;
  const serverError = mutation.isError
    ? mutation.error instanceof ApiError
      ? mutation.error.message
      : t(initial ? "knowledgeBases.error.update" : "knowledgeBases.error.create")
    : null;
  // 同じ名前の KB がある（409）ときは、作成・更新とも名前の欄の下に理由を出す。ほかの失敗は欄に結び付かない
  // ため、確定の行の直上の 1 か所だけに出す（messaging.md §3.3.1。#585 / #1786）。
  const conflict = mutation.error instanceof ApiError && mutation.error.status === 409;
  const nameError =
    (!isDefault && touched.name ? validateKnowledgeBaseName(draft.name) : null) ??
    (conflict ? serverError : null);
  // 説明は必須（#521）。説明が空の既存の KB は、保存するときに入力を求める。
  const descriptionError = touched.description
    ? validateKnowledgeBaseDescription(draft.description)
    : null;

  // 左上の「一覧へ戻る」は、エディタのフォームだけでなくページのほかの未保存（知識の節・構築設定など）も
  // リンク・戻るボタンと同じく 1 回だけ確認する（#586 / #618）。
  const back = async () => {
    if (await confirmPendingLeave()) onBack();
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isArchived) return;
    setTouched({ name: true, description: true });
    const errors = [
      [NAME_FIELD_ID, isDefault ? null : validateKnowledgeBaseName(draft.name)],
      [DESCRIPTION_FIELD_ID, validateKnowledgeBaseDescription(draft.description)],
    ] as const;
    if (firstInvalidFieldId(errors)) {
      focusFirstInvalidField(errors);
      return;
    }
    const saved = draft;
    const onConflict = (error: unknown) => {
      if (error instanceof ApiError && error.status === 409) {
        document.getElementById(NAME_FIELD_ID)?.focus();
      }
    };
    if (initial) {
      update.mutate(
        {
          id: initial.id,
          payload: {
            ...(isDefault ? {} : { name: saved.name.trim() }),
            description: saved.description.trim(),
          },
        },
        {
          onSuccess: () => {
            // 保存した値を基準にして dirty を判定し直す（下書きも消える）。
            editor.markSaved(saved);
            toast.success(t("knowledgeBases.toast.updated"));
          },
          onError: onConflict,
        }
      );
      return;
    }
    create.mutate(
      {
        name: saved.name.trim(),
        description: saved.description.trim(),
        default_search_mode: "hybrid",
        retrieval_config: {},
      },
      {
        onSuccess: (detail) => {
          editor.markSaved(saved);
          toast.success(t("knowledgeBases.toast.created"));
          onCreated?.(detail.id);
        },
        onError: onConflict,
      }
    );
  };

  const title = initial?.name ?? t("knowledgeBases.create.title");

  return (
    <div>
      <PageHeader
        wide
        title={title}
        status={initial ? <KnowledgeBaseStatusPill status={initial.status} /> : undefined}
        subtitle={initial?.description || t("knowledgeBases.subtitle")}
        meta={initial ? <KnowledgeBaseMeta knowledgeBase={initial} /> : undefined}
        // 一覧へ戻るは左上（#618）。確定（作成・保存と変更を破棄）はフォームの末尾の FormActionBar（#1786）。
        back={{
          label: t("common.backToList"),
          ariaLabel: t("editor.backToListOf", { list: t("nav.knowledgeBases") }),
          onClick: () => void back(),
          testId: "editor-back",
        }}
      />
      <PageBody wide className="grid grid-cols-1 gap-5">
        {isArchived ? <Banner severity="warning">{t("knowledgeBases.archivedReadonly")}</Banner> : null}
        <Card>
          <CardHeader className="flex-row flex-wrap items-start justify-between gap-3">
            <CardTitle className="flex items-center gap-2">
              <Library size={20} className="text-accent-fg" aria-hidden />
              {t("knowledgeBases.form.title")}
            </CardTitle>
            {initial ? (
              <ObjectActionBar
                actions={actionsFor(initial)}
                ariaLabel={t("common.objectActions.aria", { name: initial.name })}
                moreLabel={t("common.objectActions.more")}
                testId="knowledge-base-detail-actions"
              />
            ) : null}
          </CardHeader>
          <CardContent>
            <form
              ref={formRef}
              onSubmit={handleSubmit}
              className="space-y-4"
              aria-label={t("knowledgeBases.form.title")}
              data-testid="knowledge-base-form"
            >
              {editor.restored ? <FormStatus tone="info" message={t("editor.draftRestored")} /> : null}
              {/* 名前は名前の段（md）、説明は文章なので全幅（#1891）。 */}
              <FormGrid>
                <TextField
                  id={NAME_FIELD_ID}
                  width="md"
                  label={t("knowledgeBases.field.name")}
                  required={!isDefault && !isArchived}
                  value={draft.name}
                  onValueChange={(name) => {
                    setDraft((current) => ({ ...current, name }));
                    // 名前を直したら、同名（409）の理由は古くなるので消す。
                    if (conflict) mutation.reset();
                  }}
                  onBlur={() => setTouched((current) => ({ ...current, name: true }))}
                  // DEFAULT の名前とアーカイブ済みは変更できないので、読み取り専用にする（入力しても保存できない欄を出さない）。
                  readOnly={isDefault || isArchived}
                  aria-readonly={isDefault || isArchived || undefined}
                  helper={isDefault ? t("knowledgeBases.edit.defaultNameFixed") : undefined}
                  error={nameError ?? undefined}
                  maxLength={NAME_MAX_LENGTH}
                  autoFocus={!initial}
                  inputClassName={isDefault || isArchived ? "cursor-default text-fg-muted" : undefined}
                />
                <TextField
                  id={DESCRIPTION_FIELD_ID}
                  className="col-span-full"
                  width="full"
                  label={t("knowledgeBases.field.description")}
                  required={!isArchived}
                  value={draft.description}
                  onValueChange={(description) => setDraft((current) => ({ ...current, description }))}
                  onBlur={() => setTouched((current) => ({ ...current, description: true }))}
                  readOnly={isArchived}
                  aria-readonly={isArchived || undefined}
                  placeholder={isArchived ? undefined : t("knowledgeBases.field.descriptionPlaceholder")}
                  helper={t("knowledgeBases.field.descriptionHelper")}
                  error={descriptionError ?? undefined}
                  maxLength={DESCRIPTION_MAX_LENGTH}
                  inputClassName={isArchived ? "cursor-default text-fg-muted" : undefined}
                />
              </FormGrid>
              {/* 保存の失敗は確定の行の直上の 1 か所だけ（messaging.md §3.3.1。#585 / #1786）。 */}
              <SaveErrorBanner
                message={conflict ? null : serverError}
                attemptKey={mutation.submittedAt}
                testId="knowledge-base-save-error"
              />
              {/* 確定はフォームの末尾の左（作成・保存 → 変更を破棄）。画面の下端に貼り付けない（#1786 / #2715）。 */}
              <FormActionBar
                ariaLabel={t("editor.actions.formLabel")}
                testId="knowledge-base-form-actions"
                primaryActions={[
                  {
                    id: "save",
                    label: initial ? t("knowledgeBases.actions.save") : t("knowledgeBases.actions.create"),
                    icon: initial ? Save : Database,
                    type: "submit",
                    loading: pending,
                    disabled: isArchived,
                  },
                ]}
                secondaryActions={
                  isArchived
                    ? []
                    : [
                        {
                          id: "discard",
                          label: t("editor.actions.discard"),
                          icon: RotateCcw,
                          disabled: !dirty || pending,
                          onClick: () => {
                            editor.discard();
                            setTouched({ name: false, description: false });
                          },
                        },
                      ]
                }
              />
            </form>
          </CardContent>
        </Card>
        {children}
      </PageBody>
    </div>
  );
}

/** ヘッダーの件数（文書・索引済み・エラー）と更新日時。エラーがあるときは色だけでなくアイコン付きのバッジで示す。 */
function KnowledgeBaseMeta({ knowledgeBase }: { knowledgeBase: KnowledgeBaseDetail }) {
  return (
    <span className="tnum flex flex-wrap items-center gap-x-3 gap-y-1" data-testid="knowledge-base-meta">
      <span>{t("knowledgeBases.meta.documents", { count: formatNumber(knowledgeBase.document_count) })}</span>
      <span>
        {t("knowledgeBases.meta.indexed", { count: formatNumber(knowledgeBase.indexed_document_count) })}
      </span>
      {knowledgeBase.error_document_count > 0 ? (
        <StatusBadge
          variant="danger"
          label={t("knowledgeBases.meta.errors", {
            count: formatNumber(knowledgeBase.error_document_count),
          })}
        />
      ) : (
        <span>
          {t("knowledgeBases.meta.errors", { count: formatNumber(knowledgeBase.error_document_count) })}
        </span>
      )}
      <span>{t("editor.meta.updated", { date: formatDateTime(knowledgeBase.updated_at) })}</span>
    </span>
  );
}
