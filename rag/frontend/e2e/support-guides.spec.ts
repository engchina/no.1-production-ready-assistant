import { readFile } from "node:fs/promises";
import { expect, type Locator, type Page, type Route, test } from "./fixtures/test";
import { findClippedPlaceholders } from "../../../platform/packages/ui/e2e/layout-checks";
import { expectNoPageOverflow, expectSearchablePanelFits, mockDatabaseReady, mockLocalAuth } from "./_helpers";

// 業務ガイド（#1237）: 検索・回答プロファイルの知識の「業務ガイド」タブ。
// 一覧・作成・保存・検証（問題 / 注意）・公開・409 の競合・履歴とロールバック・取り込みの確認。
// desktop / mobile（375px）の 2 つの project で動かす。

const envelope = (data: unknown) => ({ data, error_messages: [], warning_messages: [] });
const failure = (status: number, messages: string[]) => ({
  status,
  json: { data: null, error_messages: messages, warning_messages: [] },
});

const profileSummary = {
  id: "bv-1",
  name: "受注サポート",
  description: null,
  status: "ACTIVE",
  knowledge_base_count: 1,
  created_at: "2026-09-25T00:00:00Z",
  updated_at: "2026-09-25T00:00:00Z",
  archived_at: null,
};

const profileDetail = {
  ...profileSummary,
  config: {
    version: 1,
    knowledge_base_ids: ["kb-1"],
    query: { guardrail_policy: null, answer_engine: "grounded" },
  },
  knowledge_bases: [{ id: "kb-1", name: "受注マニュアル" }],
};

type Content = Record<string, unknown> & { title: string };
type Issue = { severity: "error" | "warning"; code: string; path: string; message: string };
type Revision = {
  revision: number;
  title: string;
  content_sha256: string;
  published_at: string;
  published_by: string | null;
  rollback_from: number | null;
  content: Content;
};
type Guide = {
  guide_id: string;
  status: "active" | "archived";
  draft_revision: number;
  draft: Content;
  published_revision: number | null;
  revisions: Revision[];
};

function content(title: string, overrides: Partial<Content> = {}): Content {
  return {
    schema_version: 2,
    title,
    description: "",
    goal: { expected_result: "手順を終えられる", intent_examples: ["パスワードを忘れた"], match_terms: [] },
    applicability: {
      business_domains: [],
      object_types: [],
      versions: [],
      effective_from: null,
      effective_to: null,
    },
    conditions: [],
    steps: [
      {
        id: "step1",
        title: "本人を確かめる",
        purpose: "",
        depends_on: [],
        retrieval_hints: [],
        evidence_requirements: [],
        allowed_tools: ["rag_search"],
        done_when: "",
        references: [],
      },
    ],
    branches: [],
    references: [],
    completion: [],
    impact: { scope: "individual", approval_required: false, approval_note: "", steps: [] },
    handoff: { conditions: [], contact: "" },
    ...overrides,
  };
}

const coverageWarning: Issue = {
  severity: "warning",
  code: "branch_not_covered",
  path: "conditions[0]",
  message: "条件「アカウントの種類」の「派遣」に当たる分岐がありません。",
};
const referenceError: Issue = {
  severity: "error",
  code: "reference_unavailable",
  path: "references[0]",
  message: "資料「運用手順書」: 索引が済んでいません。",
};

/** 公開の履歴に残す版の数（backend の RAG_SUPPORT_GUIDE_MAX_PUBLISHED_REVISIONS の既定。#2356）。 */
const MAX_PUBLISHED_REVISIONS = 5;

/** 業務ガイドの API の状態を持つ mock（ほかの知識の API と検索・回答プロファイルも返す）。 */
class SupportGuideApi {
  guides: Guide[] = [];
  /** 検証で参照の問題を返すか（資料が索引済みになったら false）。 */
  referenceBroken = true;
  /** 次の下書きの保存を 409 にする（ほかの人が先に保存した）。 */
  conflictNext = false;
  requests: { method: string; path: string; body: unknown }[] = [];
  /** 資料の選択が文書の一覧に渡したナレッジベース（#2749）。 */
  documentQueries: (string | null)[] = [];
  /** 継承するシステム設定の窓口と、プロファイルの保存済みの窓口の上書き（#2683）。 */
  inheritedContact = { enabled: true, contact: "窓口 A" };
  contactOverride = "";
  /** 下書きで試したとき、この下書きのガイドで答えたか（#1288）。 */
  tryUsesDraft = true;
  /** 下書きで試す応答を遅らせる時間（ms。処理中の表示を確かめる）。 */
  tryDelayMs = 0;

  detail(guide: Guide) {
    const published = guide.revisions.find((item) => item.revision === guide.published_revision);
    const draftJson = JSON.stringify(guide.draft);
    return {
      guide_id: guide.guide_id,
      search_answer_profile_id: "bv-1",
      status: guide.status,
      title: guide.draft.title,
      draft_revision: guide.draft_revision,
      published_revision: guide.published_revision,
      has_unpublished_changes: !published || JSON.stringify(published.content) !== draftJson,
      updated_at: "2026-10-07T01:00:00Z",
      updated_by: "管理者",
      draft: guide.draft,
      published: published?.content ?? null,
      revisions: [...guide.revisions]
        .sort((a, b) => b.revision - a.revision)
        .map(({ content: _content, ...rest }) => rest),
      max_published_revisions: MAX_PUBLISHED_REVISIONS,
      issues: this.contentIssues(guide.draft),
    };
  }

  contentIssues(draft: Content): Issue[] {
    return Array.isArray(draft.branches) && draft.branches.length > 0 ? [coverageWarning] : [];
  }

  summary(guide: Guide) {
    const {
      draft: _draft,
      published: _published,
      revisions: _revisions,
      max_published_revisions: _max,
      issues: _issues,
      ...rest
    } = this.detail(guide);
    return rest;
  }

  publish(guide: Guide, rollbackFrom: number | null = null) {
    const revision = (guide.revisions.at(-1)?.revision ?? 0) + 1;
    guide.revisions.push({
      revision,
      title: guide.draft.title,
      content_sha256: `sha-${revision}`,
      published_at: `2026-10-0${Math.min(revision, 9)}T02:00:00Z`,
      published_by: "管理者",
      rollback_from: rollbackFrom,
      content: guide.draft,
    });
    guide.published_revision = revision;
    // backend と同じく、公開した版を残して直近の版だけを保存する（番号は振り直さない。#2356）。
    guide.revisions = guide.revisions.slice(-MAX_PUBLISHED_REVISIONS);
  }

  async handle(route: Route) {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname;
    const json = (request.headers()["content-type"] ?? "").includes("application/json");
    const body = json && request.postData() ? JSON.parse(request.postData() as string) : null;
    const base = "/api/search-answer-profiles/bv-1/support-guides";
    if (path.endsWith("/runtime-knowledge/edit")) {
      this.requests.push({ method, path, body });
      return route.fulfill({ json: envelope({ search_answer_profile_id: "bv-1", terms: [], rules: [] }) });
    }
    if (!path.startsWith(base)) return this.handleOther(route, path);
    this.requests.push({ method, path, body });
    const rest = path.slice(base.length);
    if (rest === "" && method === "GET") {
      const includeArchived = new URL(request.url()).searchParams.get("include_archived") === "true";
      return route.fulfill({
        json: envelope(
          this.guides
            .filter((guide) => includeArchived || guide.status === "active")
            .map((guide) => this.summary(guide)),
        ),
      });
    }
    if (rest === "" && method === "POST") {
      const guide: Guide = {
        guide_id: `guide-${this.guides.length + 1}`,
        status: "active",
        draft_revision: 1,
        draft: body.draft,
        published_revision: null,
        revisions: [],
      };
      this.guides.push(guide);
      return route.fulfill({ status: 201, json: envelope(this.detail(guide)) });
    }
    if (rest === "/import/process/workbook") {
      // 回答の過程の Excel（#2397）。列を選んでいれば選んだ列で読んだ行を返す。
      const form = request.postData() ?? "";
      this.requests.push({ method, path, body: { chosen: form.includes('name="question_column"') } });
      const sheets = [
        {
          name: "問い合わせ",
          columns: [
            { index: 0, letter: "A", header: "番号" },
            { index: 1, letter: "B", header: "質問" },
            { index: 2, letter: "C", header: "回答の過程" },
          ],
          row_count: 2,
        },
      ];
      const rows = form.includes('name="question_column"')
        ? [
            { row: 2, question: "画面Aで通知が出ない", process: "【回答の手順】設定画面Aを開く", standard_answer: "" },
            { row: 3, question: "画面Bの出力", process: "【検索する資料】資料B", standard_answer: "" },
          ]
        : [];
      return route.fulfill({
        json: envelope({
          file_name: "process.xlsx",
          sheets,
          sheet: rows.length ? "問い合わせ" : null,
          rows,
          issues: [],
          max_rows: 30,
          truncated: false,
          next_start_row: null,
        }),
      });
    }
    if (rest === "/import/process/preview") {
      this.requests.push({ method, path, body });
      const items = (body.rows as { row: number; question: string }[]).map((row, index) =>
        index === 0
          ? {
              row: row.row,
              question: row.question,
              valid: true,
              content: content("画面Aの通知を表示する"),
              issues: [],
              references: [
                {
                  name: "01_操作手順書A.pdf",
                  pages: "12-14",
                  section: "",
                  status: "resolved",
                  document_id: "doc-a",
                  file_name: "操作手順書A.pdf",
                  candidates: [],
                },
              ],
              rule_candidates: [
                { title: "通知の質問は設定画面Aを案内する", triggers: ["通知"], content: "原因は言い切らない。" },
              ],
            }
          : {
              row: row.row,
              question: row.question,
              valid: false,
              content: content("画面Bを出力する"),
              issues: [
                {
                  severity: "error",
                  code: "reference_unresolved",
                  path: "references[0]",
                  message: "資料「資料B」は、このプロファイルのナレッジベースの文書にありません。",
                },
              ],
              references: [
                {
                  name: "資料B",
                  pages: "",
                  section: "",
                  status: "unresolved",
                  document_id: null,
                  file_name: null,
                  candidates: [],
                },
              ],
              rule_candidates: [],
            },
      );
      return route.fulfill({
        json: envelope({ items, importable_count: items.filter((item) => item.valid).length }),
      });
    }
    if (rest === "/import/preview") {
      const items = (body.guides as Content[]).map((item, index) => {
        const valid = typeof item.goal === "object" && item.goal !== null;
        // 同じ名前の既存のガイドとの差分（#1288。backend の diff_contents の形）。
        const existing = valid ? this.guides.find((guide) => guide.draft.title === item.title) : undefined;
        return {
          index,
          title: item.title ?? null,
          valid,
          issues: valid
            ? []
            : [{ severity: "error", code: "invalid_content", path: "goal", message: "入力してください。" }],
          existing: existing
            ? {
                guide_id: existing.guide_id,
                title: existing.draft.title,
                matched_by: "title",
                base: existing.published_revision != null ? "published" : "draft",
                revision: existing.published_revision ?? existing.draft_revision,
                status: existing.status,
                changes: [
                  { section: "basic", kind: "changed", key: "", label: item.title, fields: ["description"] },
                  {
                    section: "conditions",
                    kind: "changed",
                    key: "account",
                    label: "アカウントの種類",
                    fields: ["allowed_values", "order"],
                  },
                  { section: "steps", kind: "added", key: "notify", label: "利用者に知らせる", fields: [] },
                  { section: "steps", kind: "removed", key: "step1", label: "本人を確かめる", fields: [] },
                  { section: "impact", kind: "changed", key: "", label: "", fields: ["scope"] },
                ],
              }
            : null,
        };
      });
      return route.fulfill({
        json: envelope({ items, importable_count: items.filter((item) => item.valid).length }),
      });
    }
    if (rest === "/import") {
      const created = (body.guides as Content[]).map((draft) => {
        const guide: Guide = {
          guide_id: `guide-${this.guides.length + 1}`,
          status: "active",
          draft_revision: 1,
          draft,
          published_revision: null,
          revisions: [],
        };
        this.guides.push(guide);
        return this.summary(guide);
      });
      return route.fulfill({ json: envelope({ created }) });
    }
    const match = /^\/([^/]+)(\/.*)?$/.exec(rest);
    const guide = this.guides.find((item) => item.guide_id === match?.[1]);
    if (!guide) return route.fulfill(failure(404, ["業務ガイドが見つかりません。"]));
    const action = match?.[2] ?? "";
    if (action === "" && method === "GET") return route.fulfill({ json: envelope(this.detail(guide)) });
    if (action === "" && method === "PUT") {
      if (this.conflictNext || body.base_revision !== guide.draft_revision) {
        this.conflictNext = false;
        guide.draft = content("別の人が直したタイトル");
        guide.draft_revision += 1;
        return route.fulfill(
          failure(409, [
            `読み込んだ後にほかの人が業務ガイドを保存しました。最新の内容（版 ${guide.draft_revision}）を読み込み直してから保存してください。`,
          ]),
        );
      }
      guide.draft = body.draft;
      guide.draft_revision += 1;
      return route.fulfill({ json: envelope(this.detail(guide)) });
    }
    if (action === "/try") {
      // 下書きで試す（#1288）。公開の版・下書きは変えない。
      if (body.draft_revision !== guide.draft_revision) {
        return route.fulfill(
          failure(409, [
            `読み込んだ後にほかの人が業務ガイドを保存しました。最新の内容（版 ${guide.draft_revision}）を読み込み直してから試してください。`,
          ]),
        );
      }
      if (this.tryDelayMs) await new Promise((resolve) => setTimeout(resolve, this.tryDelayMs));
      const conditions = body.conditions as Record<string, string>;
      return route.fulfill({
        json: envelope({
          trace_id: "guide-preview-0001",
          guide_id: guide.guide_id,
          draft_revision: guide.draft_revision,
          published_revision: guide.published_revision,
          guide_used: this.tryUsesDraft,
          guide: this.tryUsesDraft
            ? {
                guide_id: guide.guide_id,
                revision: guide.draft_revision,
                title: guide.draft.title,
                decision: conditions.account ? "answer" : "branch",
                known_conditions: conditions.account
                  ? [{ id: "account", label: "アカウントの種類", value: conditions.account, source: "user" }]
                  : [],
                unknown_conditions: conditions.account ? [] : [{ id: "account", label: "アカウントの種類" }],
                applicability: {},
                draft: true,
              }
            : null,
          outcome: "answered",
          notice: "変更前に管理窓口へ確認してください。",
          answer: "社員の場合は、ポータルの「パスワードを忘れた」から再設定します。",
          citations: [
            {
              document_id: "doc-1",
              chunk_id: "doc-1:c1",
              text: "パスワードを忘れた場合は、ログイン画面のリンクから再設定する。",
              score: 0.91,
              rerank_score: null,
              file_name: "運用手順書.pdf",
              category_name: null,
              metadata: {},
            },
          ],
          clarifications: [],
          // 手順ごとの充足（#2686）。手順 2 は原文だけ・分岐の手順 3 は必須ではない・手順 4 は手順ごとの検索を
          // 打ち切って検索しなかった（#2706）。
          steps: this.tryUsesDraft
            ? [
                { step_id: "step1", title: "本人であることを確かめる", status: "explained", required: true },
                { step_id: "step2", title: "再設定の画面を案内する", status: "reference_only", required: true },
                { step_id: "step3", title: "別の窓口を案内する", status: "unused", required: false },
                { step_id: "step4", title: "完了を知らせる", status: "not_searched", required: true },
                { step_id: "step5", title: "設定を確かめる", status: "unknown", required: true },
                { step_id: "step6", title: "結果を確認する", status: "partial", required: true },
              ]
            : [],
          elapsed_ms: 4200,
        }),
      });
    }
    if (action === "/validate") {
      const issues = [...(this.referenceBroken ? [referenceError] : []), ...this.contentIssues(guide.draft)];
      return route.fulfill({
        json: envelope({ valid: !issues.some((issue) => issue.severity === "error"), issues }),
      });
    }
    if (action === "/publish") {
      if (this.referenceBroken) {
        return route.fulfill(
          failure(422, ["検証で問題が見つかったため公開できません。", referenceError.message]),
        );
      }
      this.publish(guide);
      return route.fulfill({ json: envelope(this.detail(guide)) });
    }
    const revisionMatch = /^\/revisions\/(\d+)$/.exec(action);
    if (revisionMatch) {
      const revision = guide.revisions.find((item) => item.revision === Number(revisionMatch[1]));
      return revision
        ? route.fulfill({ json: envelope(revision) })
        : route.fulfill(failure(404, ["業務ガイドが見つかりません。"]));
    }
    if (action === "/rollback") {
      const target = guide.revisions.find((item) => item.revision === body.revision);
      if (!target) return route.fulfill(failure(404, ["業務ガイドが見つかりません。"]));
      // 下書きを置き換えるので、読み込んだ下書きの版を照合する（#1278）。
      if (this.conflictNext || body.base_revision !== guide.draft_revision) {
        this.conflictNext = false;
        guide.draft = content("別の人が直したタイトル");
        guide.draft_revision += 1;
        return route.fulfill(
          failure(409, [
            `読み込んだ後にほかの人が業務ガイドを保存しました。最新の内容（版 ${guide.draft_revision}）を読み込み直してから戻してください。`,
          ]),
        );
      }
      guide.draft = target.content;
      guide.draft_revision += 1;
      this.publish(guide, target.revision);
      return route.fulfill({ json: envelope(this.detail(guide)) });
    }
    if (action === "/archive" || action === "/restore") {
      guide.status = action === "/archive" ? "archived" : "active";
      return route.fulfill({ json: envelope(this.detail(guide)) });
    }
    return route.fulfill(failure(404, ["見つかりません。"]));
  }

  handleOther(route: Route, path: string) {
    if (path.endsWith("/approved-faq")) {
      return route.fulfill({ json: envelope({ search_answer_profile_id: "bv-1", enabled: true, records: [] }) });
    }
    // 継承するシステム設定の窓口（#2683）。
    if (path.endsWith("/inherited-contact")) return route.fulfill({ json: envelope(this.inheritedContact) });
    if (path.endsWith("/bv-1")) {
      return route.fulfill({
        json: envelope({ ...profileDetail, config: { ...profileDetail.config, contact_override: this.contactOverride } }),
      });
    }
    return route.fulfill({
      json: envelope({ items: [profileSummary], total: 1, limit: 50, offset: 0, has_next: false }),
    });
  }
}

/** 資料に選ぶ文書（#2749）。kb-1（プロファイルのナレッジベース）の文書だけを候補に返す。 */
const referenceDocuments = [
  { id: "doc-1", file_name: "運用手順書", knowledge_base_ids: ["kb-1"] },
  { id: "doc-a", file_name: "文書 A.pdf", knowledge_base_ids: ["kb-1"] },
  { id: "doc-b", file_name: "画面の説明", knowledge_base_ids: ["kb-1"] },
  { id: "doc-s", file_name: "一覧 A.xlsx", knowledge_base_ids: ["kb-1"] },
  { id: "doc-x", file_name: "ほかの文書.pdf", knowledge_base_ids: ["kb-2"] },
].map((document) => ({
  id: document.id,
  file_name: document.file_name,
  status: "INDEXED",
  category_name: null,
  uploaded_at: "2026-10-01T00:00:00Z",
  knowledge_bases: document.knowledge_base_ids.map((id) => ({ id, name: id })),
}));

/** 資料の選択（ナレッジベースの「文書を追加」と同じ部品）を開き、文書名で選んで足す。 */
async function pickReferenceDocuments(
  page: Page,
  container: Locator,
  addButton: string,
  names: string[],
) {
  await container.getByRole("button", { name: addButton, exact: true }).click();
  const listbox = page.getByRole("listbox", { name: "追加する文書の候補" });
  for (const name of names) await listbox.getByRole("option", { name }).click();
  await page.getByRole("button", { name: `選択した ${names.length} 件を追加` }).click();
  await expect(listbox).toHaveCount(0);
}

async function openSupportGuides(page: Page, guides: Guide[] = [], setup?: (api: SupportGuideApi) => void) {
  const api = new SupportGuideApi();
  api.guides = guides;
  setup?.(api);
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({ json: envelope({ items: [], total: 0, limit: 50, offset: 0, has_next: false }) }),
  );
  await page.route("**/api/documents?**", (route) => {
    const params = new URL(route.request().url()).searchParams;
    api.documentQueries.push(params.get("knowledge_base_id"));
    const bases = (params.get("knowledge_base_id") ?? "").split(",").filter(Boolean);
    const items = referenceDocuments.filter(
      (document) => bases.length === 0 || document.knowledge_bases.some((kb) => bases.includes(kb.id)),
    );
    return route.fulfill({
      json: envelope({ items, total: items.length, limit: 100, offset: 0, has_next: false }),
    });
  });
  await page.route("**/api/search-answer-profiles**", (route) => api.handle(route));
  await page.goto("/search-answer-profiles?id=bv-1");
  await page.getByRole("tab", { name: "業務ガイド" }).click();
  await expect(page.getByRole("tab", { name: "業務ガイド" })).toHaveAttribute("aria-selected", "true");
  return api;
}

function publishedGuide(): Guide {
  const first = content("パスワードの再設定");
  const second = content("パスワードの再設定（改訂）");
  return {
    guide_id: "guide-1",
    status: "active",
    draft_revision: 3,
    draft: second,
    published_revision: 2,
    revisions: [
      {
        revision: 1,
        title: first.title,
        content_sha256: "sha-1",
        published_at: "2026-10-01T02:00:00Z",
        published_by: "管理者",
        rollback_from: null,
        content: first,
      },
      {
        revision: 2,
        title: second.title,
        content_sha256: "sha-2",
        published_at: "2026-10-02T02:00:00Z",
        published_by: "管理者",
        rollback_from: null,
        content: second,
      },
    ],
  };
}

test("空の一覧から業務ガイドを作り、保存・検証（問題と注意）・公開できる", async ({ page }) => {
  const api = await openSupportGuides(page);
  await expect(page.getByText("業務ガイドはまだありません")).toBeVisible();

  await page.getByRole("button", { name: "新しい業務ガイド" }).click();
  const editor = page.getByTestId("support-guide-editor");
  await expect(editor.getByRole("heading", { name: "新しい業務ガイド" })).toBeVisible();
  // 検証・公開は保存してから。
  await expect(editor.getByRole("button", { name: "検証", exact: true })).toBeDisabled();

  // 必須の未入力は保存の前に止め、操作の直下と欄の直下に出す。
  await editor.getByRole("button", { name: "保存", exact: true }).click();
  const result = page.getByTestId("support-guide-result");
  await expect(result.getByText("保存できない入力が 2 件あります")).toBeVisible();
  await expect(editor.getByLabel("タイトル")).toBeFocused();
  expect(api.requests.filter((item) => item.method === "POST")).toHaveLength(0);

  await editor.getByLabel("タイトル").fill("パスワードの再設定");
  await editor.getByLabel("期待する結果").fill("利用者が再設定を終えられる");
  await editor.getByLabel("質問の例").fill("パスワードを忘れた\nログインできない");

  await editor.getByRole("button", { name: "条件を足す", exact: true }).click();
  const condition = editor.getByRole("listitem", { name: "条件 1" });
  await condition.getByLabel("名前").fill("アカウントの種類");
  await condition.getByLabel("選択肢").fill("社員\n派遣");
  await condition.getByLabel("確かめる問い").fill("社員ですか、派遣ですか？");

  await editor.getByRole("button", { name: "手順を足す" }).click();
  await editor.getByRole("listitem", { name: "手順 1" }).getByLabel("題名").fill("本人を確かめる");
  await editor.getByRole("button", { name: "手順を足す" }).click();
  await editor.getByRole("listitem", { name: "手順 2" }).getByLabel("題名").fill("再設定する");

  await editor.getByRole("button", { name: "分岐を足す" }).click();
  const branch = editor.getByRole("listitem", { name: "分岐 1" });
  await branch.getByRole("combobox", { name: /^条件/ }).click();
  await page.getByRole("option", { name: "アカウントの種類（cond1）" }).click();
  await branch.getByRole("combobox", { name: /^値/ }).click();
  await page.getByRole("option", { name: "社員" }).click();
  await branch.getByRole("combobox", { name: /^行き先の手順/ }).click();
  await page.getByRole("option", { name: "再設定する（step2）" }).click();

  // 影響範囲・承認が係る手順（#1320）は、手順の題名で選ぶ。
  await editor.getByRole("combobox", { name: /^影響範囲・承認が係る手順/ }).click();
  await page.getByRole("option", { name: /再設定する/ }).click();
  await page.keyboard.press("Escape");

  await pickReferenceDocuments(page, editor, "資料を足す", ["運用手順書"]);
  const reference = editor.getByRole("listitem", { name: "資料 1", exact: true });
  await expect(reference.getByLabel("文書", { exact: true })).toHaveValue("運用手順書");

  await editor.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("業務ガイドを下書きとして作りました。")).toBeVisible();
  const created = api.requests.find((item) => item.method === "POST" && item.path.endsWith("/support-guides"));
  expect(created?.body).toMatchObject({
    draft: {
      title: "パスワードの再設定",
      goal: { intent_examples: ["パスワードを忘れた", "ログインできない"] },
      conditions: [{ id: "cond1", allowed_values: ["社員", "派遣"] }],
      branches: [{ when: { condition_id: "cond1", operator: "equals", values: ["社員"] }, goto_step: "step2" }],
      impact: { steps: ["step2"] },
      references: [{ document_id: "doc-1", title: "運用手順書" }],
    },
  });
  await expect(editor.getByRole("heading", { name: "編集中: パスワードの再設定" })).toBeVisible();
  await expect(editor.getByText("下書きのみ")).toBeVisible();
  await expect(page.getByRole("rowheader", { name: "パスワードの再設定" })).toBeVisible();
  // 保存した下書きの内容の検証（注意）を出す。
  await expect(page.getByTestId("support-guide-warnings")).toContainText("分岐がありません");

  // 検証: 参照する資料の問題（公開できない）と注意を分けて、位置を利用者の言葉で出す。
  await editor.getByRole("button", { name: "検証", exact: true }).click();
  await expect(result.getByText("公開できない問題が 1 件あります")).toBeVisible();
  await expect(page.getByTestId("support-guide-errors")).toContainText("資料 1: 資料「運用手順書」: 索引が済んでいません。");
  await expect(result.getByText("確かめてほしい点が 1 件あります")).toBeVisible();
  await expect(page.getByTestId("support-guide-warnings")).toContainText("条件 1:");

  // 問題が残ったまま公開すると、断られた理由を出す。
  await editor.getByRole("button", { name: "公開", exact: true }).click();
  await page.getByRole("alertdialog", { name: "この業務ガイドを公開しますか？" }).getByRole("button", { name: "公開" }).click();
  await expect(result.getByText("検証で問題が見つかったため公開できません。")).toBeVisible();
  await expect(page.getByTestId("support-guide-refused-issues")).toContainText("索引が済んでいません。");

  // 資料が索引済みになったら、検証に通り公開できる。
  api.referenceBroken = false;
  await editor.getByRole("button", { name: "検証", exact: true }).click();
  await expect(page.getByTestId("support-guide-errors")).toHaveCount(0);
  await editor.getByRole("button", { name: "公開", exact: true }).click();
  await page.getByRole("alertdialog", { name: "この業務ガイドを公開しますか？" }).getByRole("button", { name: "公開" }).click();
  await expect(page.getByText("業務ガイドを公開しました（版 1）。")).toBeVisible();
  await expect(editor.getByText("公開中", { exact: true }).first()).toBeVisible();
  await expect(editor.getByRole("button", { name: "公開", exact: true })).toBeDisabled();
  await expect(page.getByRole("rowheader", { name: "版 1 公開中" })).toBeVisible();
  await expectNoPageOverflow(page);
});

// #2395: 参照する資料の頁の範囲と、手順ごとの参照（資料と頁）を入れて保存する。頁の形の誤りは欄の直下に出す。
test("参照する資料の頁と手順ごとの参照を入れて保存できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const api = await openSupportGuides(page);
  await page.getByRole("button", { name: "新しい業務ガイド" }).click();
  const editor = page.getByTestId("support-guide-editor");
  await editor.getByLabel("タイトル").fill("設定の変更");
  await editor.getByLabel("期待する結果").fill("設定を変えて反映できる");
  await editor.getByLabel("質問の例").fill("設定を変えたい");

  await editor.getByRole("button", { name: "手順を足す" }).click();
  await editor.getByRole("listitem", { name: "手順 1", exact: true }).getByLabel("題名").fill("画面 A で変える");
  await editor.getByRole("button", { name: "手順を足す" }).click();
  const second = editor.getByRole("listitem", { name: "手順 2", exact: true });
  await second.getByLabel("題名").fill("画面 B で反映する");
  const stepReferences = second.getByRole("group", { name: "この手順が参照する資料" });
  await expect(stepReferences).toHaveAccessibleDescription(/この頁に絞って根拠を探し/);
  // 資料は、ナレッジベースの「文書を追加」と同じ選択で、プロファイルのナレッジベースの文書から選ぶ（#2749）。
  await stepReferences.getByRole("button", { name: "この手順の資料を足す" }).click();
  const picker = page.getByTestId("support-guide-step-reference-picker-1");
  const listbox = picker.getByRole("listbox", { name: "追加する文書の候補" });
  await expect(picker.getByRole("searchbox", { name: "追加する文書を検索" })).toBeFocused();
  await expect(listbox.getByRole("option", { name: "ほかの文書.pdf" })).toHaveCount(0);
  expect(api.documentQueries.at(-1)).toBe("kb-1");
  await expectNoPageOverflow(page);
  await picker.screenshot({ path: testInfo.outputPath("support-guide-reference-picker.png") });
  await listbox.getByRole("option", { name: "画面の説明" }).click();
  await picker.getByRole("button", { name: "選択した 1 件を追加" }).click();
  await expect(picker).toHaveCount(0);
  const stepReference = stepReferences.getByRole("listitem", { name: "手順 2 の資料 1" });
  await expect(stepReference.getByLabel("文書", { exact: true })).toHaveValue("画面の説明");
  await expect(stepReference.getByLabel("文書", { exact: true })).toHaveAccessibleDescription("文書 ID: doc-b");
  await stepReference.getByLabel("根拠の頁").fill("6-4");
  await stepReference.screenshot({ path: testInfo.outputPath("support-guide-step-reference.png") });
  // すでに入れた文書は「追加済み」で選べない。
  await stepReferences.getByRole("button", { name: "この手順の資料を足す" }).click();
  const added = picker.getByRole("option", { name: "画面の説明" });
  await expect(added).toHaveAttribute("aria-disabled", "true");
  await expect(added).toContainText("追加済み");
  await picker.getByRole("button", { name: "閉じる" }).click();
  await expect(picker).toHaveCount(0);

  await pickReferenceDocuments(page, editor, "資料を足す", ["文書 A.pdf"]);
  const reference = editor.getByRole("listitem", { name: "資料 1", exact: true });
  await reference.getByLabel("根拠の頁").fill("2-3");

  // 頁の形の誤り（始まりより前の終わり）は保存の前に止め、欄の直下に出す。
  await editor.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByTestId("support-guide-result").getByText("保存できない入力が 1 件あります")).toBeVisible();
  await expect(stepReference.getByText("頁は「3-5, 8」の形で入れてください", { exact: false })).toBeVisible();
  expect(api.requests.filter((item) => item.method === "POST")).toHaveLength(0);

  await stepReference.getByLabel("根拠の頁").fill("4-6、9");
  await editor.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("業務ガイドを下書きとして作りました。")).toBeVisible();
  const created = api.requests.find((item) => item.method === "POST" && item.path.endsWith("/support-guides"));
  expect(created?.body).toMatchObject({
    draft: {
      schema_version: 2,
      steps: [
        { id: "step1", references: [] },
        {
          id: "step2",
          references: [
            {
              document_id: "doc-b",
              title: "画面の説明",
              page_ranges: [
                { page_start: 4, page_end: 6 },
                { page_start: 9, page_end: 9 },
              ],
            },
          ],
        },
      ],
      references: [{ document_id: "doc-a", title: "文書 A.pdf", page_ranges: [{ page_start: 2, page_end: 3 }] }],
    },
  });
  // 読み込み直した下書きでも同じ形で出す。
  await expect(editor.getByRole("listitem", { name: "手順 2 の資料 1" }).getByLabel("根拠の頁")).toHaveValue("4-6, 9");

  // 手順の資料を外せる。
  await editor.getByRole("button", { name: "手順 2 の資料 1 を外す" }).click();
  await expect(editor.getByRole("listitem", { name: "手順 2 の資料 1" })).toHaveCount(0);
  await expectNoPageOverflow(page);
});

// #2663: 表計算の資料はシートと行で参照し、頁と同時には入れない（行の番号を頁にしない）。
test("表計算の資料をシートと行で参照して保存できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const api = await openSupportGuides(page);
  await page.getByRole("button", { name: "新しい業務ガイド" }).click();
  const editor = page.getByTestId("support-guide-editor");
  await editor.getByLabel("タイトル").fill("設定の値を確かめる");
  await editor.getByLabel("期待する結果").fill("設定の値が分かる");
  await editor.getByLabel("質問の例").fill("設定の値を知りたい");
  await editor.getByRole("button", { name: "手順を足す" }).click();
  const step = editor.getByRole("listitem", { name: "手順 1", exact: true });
  await step.getByLabel("題名").fill("一覧で値を確かめる");
  const stepReferences = step.getByRole("group", { name: "この手順が参照する資料" });
  await pickReferenceDocuments(page, stepReferences, "この手順の資料を足す", ["一覧 A.xlsx"]);
  const stepReference = stepReferences.getByRole("listitem", { name: "手順 1 の資料 1" });
  const sheets = stepReference.getByLabel("根拠のシートと行（表計算）");
  await expect(sheets).toHaveAccessibleDescription(/シート名!42-45/);
  for (const invalid of ["Sheet1!A1048577", "Sheet1!XFE1", "Sheet1!D10:B3"]) {
    await sheets.fill(invalid);
    await editor.getByRole("button", { name: "保存", exact: true }).click();
    await expect(stepReference.getByText(/シートと行は 1 行に 1 つ/)).toBeVisible();
    expect(api.requests.filter((item) => item.method === "POST")).toHaveLength(0);
  }
  await sheets.fill("シートA!42-45\nシートB!B3:D10");
  // 頁と同時に入れたら保存の前に止め、欄の直下に出す。
  await stepReference.getByLabel("根拠の頁").fill("42");
  await editor.getByRole("button", { name: "保存", exact: true }).click();
  await expect(stepReference.getByText("根拠の頁かシートと行のどちらかだけ", { exact: false })).toBeVisible();
  expect(api.requests.filter((item) => item.method === "POST")).toHaveLength(0);
  await expectNoPageOverflow(page);
  await stepReference.screenshot({ path: testInfo.outputPath("support-guide-sheet-reference.png") });

  await stepReference.getByLabel("根拠の頁").fill("");
  await editor.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("業務ガイドを下書きとして作りました。")).toBeVisible();
  const created = api.requests.find((item) => item.method === "POST" && item.path.endsWith("/support-guides"));
  expect(created?.body).toMatchObject({
    draft: {
      steps: [
        {
          id: "step1",
          references: [
            {
              document_id: "doc-s",
              page_ranges: [],
              sheet_ranges: [
                { sheet_name: "シートA", row_start: 42, row_end: 45 },
                { sheet_name: "シートB", row_start: 3, row_end: 10, cell_range: "B3:D10" },
              ],
            },
          ],
        },
      ],
    },
  });
  await expect(
    editor.getByRole("listitem", { name: "手順 1 の資料 1" }).getByLabel("根拠のシートと行（表計算）"),
  ).toHaveValue("シートA!42-45\nシートB!B3:D10");
  await expectNoPageOverflow(page);
});

// #2091: 業務ガイドの検索つきの複数選択（依存する手順・分岐の値・影響範囲・承認が係る手順）の開いた一覧の下部
// （N 件選択中・表示中をすべて選択・クリア・完了）が 1 行に並び、画面からはみ出さない。
test("業務ガイドの複数選択の開いた一覧の下部が 1 行に並び、画面からはみ出さない", { tag: "@responsive" }, async ({ page }) => {
  await openSupportGuides(page);
  await page.getByRole("button", { name: "新しい業務ガイド" }).click();
  const editor = page.getByTestId("support-guide-editor");

  await editor.getByRole("button", { name: "条件を足す", exact: true }).click();
  const condition = editor.getByRole("listitem", { name: "条件 1" });
  await condition.getByLabel("名前").fill("条件 A");
  await condition.getByLabel("選択肢").fill("値 1\n値 2\n値 3");
  await editor.getByRole("button", { name: "手順を足す" }).click();
  await editor.getByRole("listitem", { name: "手順 1" }).getByLabel("題名").fill("手順 A");
  await editor.getByRole("button", { name: "手順を足す" }).click();
  const second = editor.getByRole("listitem", { name: "手順 2" });
  await second.getByLabel("題名").fill("手順 B");

  const footer = page.locator("[data-searchable-panel-footer]");
  const check = async (combobox: ReturnType<typeof editor.getByRole>) => {
    await combobox.click();
    await expect(footer).toContainText(/\d+ 件選択中/);
    await expectSearchablePanelFits(page);
    await expectNoPageOverflow(page);
    await footer.getByRole("button", { name: "完了" }).click();
    await expect(footer).toHaveCount(0);
  };

  await check(second.getByRole("combobox", { name: /^依存する手順/ }));

  await editor.getByRole("button", { name: "分岐を足す" }).click();
  const branch = editor.getByRole("listitem", { name: "分岐 1" });
  await branch.getByRole("combobox", { name: /^条件/ }).click();
  await page.getByRole("option", { name: /条件 A/ }).click();
  await branch.getByRole("combobox", { name: /^比べ方/ }).click();
  await page.getByRole("option", { name: "いずれかに一致する" }).click();
  await check(branch.getByRole("combobox", { name: /^値/ }));

  await check(editor.getByRole("combobox", { name: /^影響範囲・承認が係る手順/ }));
});

test("ほかの人が先に保存していたら、読み込み直しを案内し、最新の内容を読み込める", async ({ page }) => {
  const api = await openSupportGuides(page, [publishedGuide()]);
  await page.getByRole("button", { name: "パスワードの再設定（改訂） を編集" }).click();
  const editor = page.getByTestId("support-guide-editor");
  await expect(editor.getByText("未公開の変更あり")).toHaveCount(0);
  await editor.getByLabel("タイトル").fill("手元で直したタイトル");
  // 保存していない変更があるうちは、検証・公開できない理由を出す。
  await expect(page.getByTestId("support-guide-save-first")).toBeVisible();

  api.conflictNext = true;
  await editor.getByRole("button", { name: "保存", exact: true }).click();
  const result = page.getByTestId("support-guide-result");
  await expect(result.getByText("ほかの人が先に保存しました")).toBeVisible();
  await expect(result).toContainText("最新の内容（版 4）を読み込み直してから保存してください。");

  await result.getByRole("button", { name: "最新の内容を読み込む" }).click();
  const dialog = page.getByRole("alertdialog", { name: "保存していない変更を破棄しますか？" });
  await dialog.getByRole("button", { name: "最新の内容を読み込む" }).click();
  await expect(page.getByText("最新の内容を読み込みました。")).toBeVisible();
  await expect(editor.getByLabel("タイトル")).toHaveValue("別の人が直したタイトル");
  await expect(page.getByTestId("support-guide-save-first")).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("公開の履歴から前の版を見て、その版に戻せる", async ({ page }) => {
  const api = await openSupportGuides(page, [publishedGuide()]);
  await page.getByRole("button", { name: "パスワードの再設定（改訂） を編集" }).click();
  const editor = page.getByTestId("support-guide-editor");
  await expect(editor.getByRole("heading", { name: "公開の履歴" })).toBeVisible();
  await expect(page.getByRole("rowheader", { name: "版 2 公開中" })).toBeVisible();

  await page.getByRole("button", { name: "版 1 の操作" }).click();
  await page.getByRole("menuitem", { name: "この版を見る" }).click();
  const view = page.getByTestId("support-guide-revision-view");
  await expect(view.getByRole("heading", { name: "版 1 の内容" })).toBeVisible();
  await expect(view.getByTestId("support-guide-content-view")).toContainText("パスワードの再設定");
  await expect(view.getByTestId("support-guide-content-view")).toContainText("本人を確かめる");

  await view.getByRole("button", { name: "この版に戻す" }).click();
  const dialog = page.getByRole("alertdialog", { name: "版 1 に戻しますか？" });
  await expect(dialog).toContainText("版 1 の内容を新しい版として公開します。");
  await dialog.getByRole("button", { name: "この版に戻す" }).click();
  await expect(page.getByText("版 1 の内容を版 3 として公開しました。")).toBeVisible();
  expect(api.requests.find((item) => item.path.endsWith("/rollback"))?.body).toEqual({
    revision: 1,
    base_revision: 3,
  });
  await expect(page.getByRole("rowheader", { name: "版 3 公開中" })).toBeVisible();
  await expect(page.getByText("版 1 に戻した")).toBeVisible();
  await expect(editor.getByLabel("タイトル")).toHaveValue("パスワードの再設定");
  await expectNoPageOverflow(page);
});

test("公開の履歴は直近 5 版を残し、前の版に戻すと古い版から消える（#2356）", async ({ page }, testInfo) => {
  const base = publishedGuide();
  const revisions = Array.from({ length: 5 }, (_, index) => ({
    ...base.revisions[0],
    revision: index + 1,
    title: `ガイド A の版 ${index + 1}`,
    content_sha256: `sha-${index + 1}`,
    published_at: `2026-10-0${index + 1}T02:00:00Z`,
    content: content(`ガイド A の版 ${index + 1}`),
  }));
  await openSupportGuides(page, [
    { ...base, draft: revisions[4].content, draft_revision: 1, published_revision: 5, revisions },
  ]);
  await page.getByRole("button", { name: "ガイド A の版 5 を編集" }).click();
  const editor = page.getByTestId("support-guide-editor");
  // 上限は API の値（max_published_revisions）で案内する。
  await expect(editor).toContainText("直近 5 版を保存します（公開中の版は常に残ります）。");
  await expect(page.getByTestId("support-guide-history-toolbar")).toContainText("全 5 件");
  await expect(page.getByRole("rowheader", { name: "版 1" })).toBeVisible();

  await page.getByRole("button", { name: "版 2 の操作" }).click();
  await page.getByRole("menuitem", { name: "この版に戻す" }).click();
  await page.getByRole("alertdialog", { name: "版 2 に戻しますか？" }).getByRole("button", { name: "この版に戻す" }).click();
  await expect(page.getByText("版 2 の内容を版 6 として公開しました。")).toBeVisible();

  // 版 1 が消え、番号は振り直さない（版 2〜6）。
  await expect(page.getByRole("rowheader", { name: "版 6 公開中" })).toBeVisible();
  await expect(page.getByRole("rowheader", { name: "版 1", exact: true })).toHaveCount(0);
  await expect(page.getByTestId("support-guide-history-toolbar")).toContainText("全 5 件");
  await expectNoPageOverflow(page);
  await page.getByTestId("support-guide-history-toolbar").scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("support-guide-history-retention.png") });
});

test("ほかの人が先に下書きを保存していたら、前の版に戻さずに読み込み直しを案内する", async ({ page }) => {
  const api = await openSupportGuides(page, [publishedGuide()]);
  await page.getByRole("button", { name: "パスワードの再設定（改訂） を編集" }).click();
  const editor = page.getByTestId("support-guide-editor");
  await expect(page.getByRole("rowheader", { name: "版 2 公開中" })).toBeVisible();

  api.conflictNext = true;
  await page.getByRole("button", { name: "版 1 の操作" }).click();
  await page.getByRole("menuitem", { name: "この版に戻す" }).click();
  await page
    .getByRole("alertdialog", { name: "版 1 に戻しますか？" })
    .getByRole("button", { name: "この版に戻す" })
    .click();
  const result = page.getByTestId("support-guide-result");
  await expect(result.getByText("ほかの人が先に保存しました")).toBeVisible();
  await expect(result).toContainText("最新の内容（版 4）を読み込み直してから戻してください。");
  // 戻していない（公開の版は 2 のまま）。
  await expect(page.getByRole("rowheader", { name: "版 2 公開中" })).toBeVisible();
  await expect(page.getByRole("rowheader", { name: /版 3/ })).toHaveCount(0);

  await result.getByRole("button", { name: "最新の内容を読み込む" }).click();
  await expect(page.getByText("最新の内容を読み込みました。")).toBeVisible();
  await expect(editor.getByLabel("タイトル")).toHaveValue("別の人が直したタイトル");
  await expectNoPageOverflow(page);
});

test("JSON の取り込みは確認で各ガイドの可否を示し、すべて取り込めるときだけ取り込む", async ({ page }) => {
  const api = await openSupportGuides(page);
  await page.getByRole("button", { name: "取り込み" }).click();
  const panel = page.getByTestId("support-guide-import");
  const field = panel.getByRole("textbox", { name: /^JSON/ });

  // JSON として読めないときは送らずに知らせる。
  await field.fill("{");
  await panel.getByRole("button", { name: "確認" }).click();
  await expect(panel.getByText("JSON として読めません。")).toBeVisible();

  await field.fill(JSON.stringify({ schema_version: 2, guides: [content("取り込むガイド"), { title: "壊れたガイド" }] }));
  await panel.getByRole("button", { name: "確認" }).click();
  await expect(panel.getByText("2 件のうち 1 件を取り込めます。")).toBeVisible();
  await expect(panel.getByTestId("support-guide-import-item-0")).toContainText("取り込める");
  await expect(panel.getByTestId("support-guide-import-item-1")).toContainText("取り込めない");
  await expect(panel.getByTestId("support-guide-import-item-1")).toContainText("目的: 入力してください。");
  await expect(panel.getByRole("button", { name: "取り込む" })).toBeDisabled();

  await field.fill(JSON.stringify({ schema_version: 2, guides: [content("取り込むガイド")] }));
  await expect(panel.getByText("内容が変わりました。もう一度「確認」してください。")).toBeVisible();
  await panel.getByRole("button", { name: "確認" }).click();
  await expect(panel.getByText("1 件のうち 1 件を取り込めます。")).toBeVisible();
  await panel.getByRole("button", { name: "取り込む" }).click();
  await expect(page.getByText("業務ガイド 1 件を下書きとして取り込みました。")).toBeVisible();
  expect(api.requests.find((item) => item.path.endsWith("/import"))?.body).toMatchObject({
    guides: [{ title: "取り込むガイド" }],
  });
  await expect(page.getByRole("rowheader", { name: "取り込むガイド" })).toBeVisible();
  await expect(page.getByTestId("support-guide-import")).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test(
  "回答の過程の Excel から列を選んで AI で下書きを作り、選んだ行とルールの候補だけを下書きにする（#2397）",
  { tag: "@responsive" },
  async ({ page }, testInfo) => {
    const api = await openSupportGuides(page);
    await page.getByRole("button", { name: "取り込み" }).click();
    const panel = page.getByTestId("support-guide-import");
    await panel.getByRole("tab", { name: "回答の過程（Excel・テキスト）" }).click();
    const process = panel.getByTestId("support-guide-process-import");
    // 列を選ぶ前に「AI で下書きを作る」を押すと、ファイルの必須を示す。
    await process.getByRole("button", { name: "AI で下書きを作る" }).click();
    await expect(process.getByText("Excel ファイルを選んでください。")).toBeVisible();

    await process.locator("#support-guide-process-file").setInputFiles({
      name: "process.xlsx",
      mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      buffer: Buffer.from("xlsx"),
    });
    await process.getByRole("combobox", { name: "質問の列" }).click();
    await page.getByRole("option", { name: "B: 質問" }).click();
    await process.getByRole("combobox", { name: "回答の過程の列" }).click();
    await page.getByRole("option", { name: "C: 回答の過程" }).click();
    await process.getByRole("button", { name: "AI で下書きを作る" }).click();

    const result = process.getByTestId("support-guide-process-result");
    await expect(result.getByText("2 行のうち 1 件を下書きにできます。")).toBeVisible();
    const valid = result.getByTestId("support-guide-process-item-2");
    await expect(valid).toContainText("2 行目: 画面Aの通知を表示する");
    await expect(valid).toContainText("文書に対応");
    await expect(valid).toContainText("→ 操作手順書A.pdf");
    await expect(valid.getByRole("checkbox", { name: /2 行目/ })).toBeChecked();
    const invalid = result.getByTestId("support-guide-process-item-3");
    await expect(invalid).toContainText("文書が無い");
    await expect(invalid).toContainText("ナレッジベースの文書にありません");
    await expect(invalid.getByRole("checkbox", { name: /3 行目/ })).toBeDisabled();
    // ルールの候補は既定で選ばない。選んだものだけを無効のルールとして足す。
    const rule = result.getByRole("checkbox", { name: "通知の質問は設定画面Aを案内する" });
    await expect(rule).not.toBeChecked();
    await rule.check();
    await expectNoPageOverflow(page);
    await panel.screenshot({ path: testInfo.outputPath("support-guide-process-preview.png") });

    await result.getByRole("button", { name: "選んだものを下書きにする" }).click();
    await expect(page.getByText("業務ガイド 1 件を下書きにし、ルール 1 件を無効のまま足しました。")).toBeVisible();
    const preview = api.requests.find((item) => item.path.endsWith("/import/process/preview"));
    expect((preview?.body as { rows: { row: number }[] }).rows.map((row) => row.row)).toEqual([2, 3]);
    expect(api.requests.find((item) => item.path.endsWith("/support-guides/import"))?.body).toMatchObject({
      guides: [{ title: "画面Aの通知を表示する" }],
    });
    expect(api.requests.find((item) => item.path.endsWith("/runtime-knowledge/edit"))?.body).toMatchObject({
      kind: "rules",
      selected: null,
      title: "通知の質問は設定画面Aを案内する",
      labels: "通知",
      enabled: false,
    });
    await expect(page.getByRole("rowheader", { name: "画面Aの通知を表示する" })).toBeVisible();
    await expect(page.getByTestId("support-guide-import")).toHaveCount(0);
  },
);

test("貼り付けた回答の過程のテキストからも下書きを作れる（#2397）", async ({ page }) => {
  const api = await openSupportGuides(page);
  await page.getByRole("button", { name: "取り込み" }).click();
  const panel = page.getByTestId("support-guide-import");
  await panel.getByRole("tab", { name: "回答の過程（Excel・テキスト）" }).click();
  const process = panel.getByTestId("support-guide-process-import");
  await process.getByRole("button", { name: "テキストを貼り付ける" }).click();
  await process.getByRole("button", { name: "AI で下書きを作る" }).click();
  await expect(process.getByText("質問を入れてください。")).toBeVisible();
  await process.getByRole("textbox", { name: "質問" }).fill("画面Aで通知が出ない");
  await process.getByRole("textbox", { name: "回答の過程" }).fill("【回答の手順】\n設定画面Aを開く");
  await process.getByRole("button", { name: "AI で下書きを作る" }).click();
  await expect(process.getByText("1 行のうち 1 件を下書きにできます。")).toBeVisible();
  const preview = api.requests.find((item) => item.path.endsWith("/import/process/preview"));
  expect(preview?.body).toMatchObject({
    rows: [{ row: 1, question: "画面Aで通知が出ない", process: "【回答の手順】\n設定画面Aを開く" }],
  });
});

/** 公開の版 2 と、利用者に確かめる条件を足した下書き（版 3）を持つガイド。 */
function guideWithConditionDraft(): Guide {
  const guide = publishedGuide();
  guide.draft = content("パスワードの再設定（改訂）", {
    conditions: [
      {
        id: "account",
        label: "アカウントの種類",
        type: "enum",
        allowed_values: ["社員", "派遣"],
        required: true,
        source: "user",
        unknown_handling: "ask",
        question: "社員ですか、派遣ですか？",
        value_aliases: {},
      },
    ],
  });
  return guide;
}

test("下書きで試すと、公開の版を変えずに下書きで答えた回答を示す", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const api = await openSupportGuides(page, [guideWithConditionDraft()]);
  api.tryDelayMs = 400;
  await page.getByRole("button", { name: "パスワードの再設定（改訂） を編集" }).click();
  const editor = page.getByTestId("support-guide-editor");
  const panel = editor.getByTestId("support-guide-try");
  await expect(panel.getByRole("heading", { name: "下書きで試す" })).toBeVisible();
  await expect(panel).toContainText("保存した下書き（版 3）で回答を作り");

  // 質問が空なら送らずに欄の直下で知らせる。
  await panel.getByRole("button", { name: "試す", exact: true }).click();
  await expect(panel.getByText("質問を入れてください。")).toBeVisible();
  expect(api.requests.filter((item) => item.path.endsWith("/try"))).toHaveLength(0);

  // 分かっている条件を選んで試す。
  await panel.getByRole("combobox", { name: /^アカウントの種類/ }).click();
  await page.getByRole("option", { name: "社員" }).click();
  await panel.getByRole("textbox", { name: /^質問/ }).fill("パスワードを忘れた");
  await panel.getByRole("button", { name: "試す", exact: true }).click();
  await expect(panel.getByTestId("support-guide-try-processing")).toBeVisible();
  const result = panel.getByTestId("support-guide-try-answer");
  await expect(result.getByText("下書き（版 3）の業務ガイドで答えました。")).toBeVisible();
  await expect(result).toContainText("利用者の回答の履歴・フィードバック・評価には入りません。");
  await expect(result).toContainText("所要時間: 4.2 秒");
  await expect(result.getByText("手順どおりに答えた")).toBeVisible();
  await expect(result.getByTestId("support-guide-try-known")).toHaveText("アカウントの種類: 社員");
  // 手順ごとの充足（#2686）。説明できていない必須の手順の数と、手順ごとの状態を文言で示す。
  const steps = result.getByTestId("support-guide-try-steps");
  await expect(steps.getByTestId("support-guide-try-steps-missing")).toHaveText(
    "必須の手順のうち 1 件を、回答で説明できていません。",
  );
  await expect(steps.getByTestId("support-guide-try-steps-unknown")).toHaveText(
    "必須の手順のうち 2 件は、充足を確認できていません。",
  );
  await expect(steps.getByTestId("support-guide-try-steps-partial")).toHaveText(
    "必須の手順のうち 1 件は、一部のみ説明を確認しました。",
  );
  const stepRows = steps.getByTestId("support-guide-try-step");
  await expect(stepRows).toHaveCount(6);
  await expect(stepRows.nth(0)).toContainText("本人であることを確かめる");
  await expect(stepRows.nth(0)).toContainText("説明した");
  await expect(stepRows.nth(1)).toContainText("資料の原文だけ");
  await expect(stepRows.nth(2)).toContainText("説明していない");
  await expect(stepRows.nth(2)).toContainText("分岐の手順（必須ではない）");
  await expect(stepRows.nth(3)).toContainText("検索を打ち切った");
  await expect(stepRows.nth(4)).toContainText("未検証");
  await expect(stepRows.nth(5)).toContainText("一部のみ確認済み");
  await expect(steps).toBeVisible();
  await steps.screenshot({ path: testInfo.outputPath("support-guide-coverage.png") });
  await expect(result).toContainText("ポータルの「パスワードを忘れた」から再設定します。");
  const notice = result.getByRole("status").filter({ hasText: "回答の注意書き" });
  await notice.scrollIntoViewIfNeeded();
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("変更前に管理窓口へ確認してください。");
  await notice.screenshot({ path: testInfo.outputPath("support-guide-notice.png") });
  // AI に渡した件数と内訳は検索・チャットと同じ表示（#2040）。
  await expect(result.getByRole("heading", { name: /^AI に渡した 1 件/ })).toBeVisible();
  expect(api.requests.find((item) => item.path.endsWith("/try"))?.body).toEqual({
    query: "パスワードを忘れた",
    draft_revision: 3,
    conditions: { account: "社員" },
  });
  // 公開の版は 2 のまま（試しは保存しない）。
  await expect(page.getByRole("rowheader", { name: "版 2 公開中" })).toBeVisible();
  expect(api.requests.some((item) => /\/(publish|rollback)$/.test(item.path))).toBe(false);
  await expectNoPageOverflow(page);
  await panel.screenshot({ path: testInfo.outputPath("support-guide-try.png") });

  // この下書きのガイドが使われなかったときは、見直す点を出す。
  api.tryUsesDraft = false;
  api.tryDelayMs = 0;
  await panel.getByRole("button", { name: "試す", exact: true }).click();
  await expect(result.getByText("この質問では、業務ガイドを使わずに答えました。")).toBeVisible();
  await expect(result).toContainText("質問の例・照合の語・適用範囲");
  await expect(result.getByTestId("support-guide-try-steps")).toHaveCount(0);

  // 保存していない変更があるうちは、保存した下書きでしか試せないので押せない。
  await editor.getByLabel("タイトル").fill("手元で直したタイトル");
  await expect(panel.getByRole("button", { name: "試す", exact: true })).toBeDisabled();
  await expect(panel.getByTestId("support-guide-try-save-first")).toBeVisible();
});

test("取り込みの確認で、同じ名前の既存のガイドとの違いを節ごとに示す", async ({ page }, testInfo) => {
  await openSupportGuides(page, [publishedGuide()]);
  await page.getByRole("button", { name: "取り込み" }).click();
  const panel = page.getByTestId("support-guide-import");
  await panel
    .getByRole("textbox", { name: /^JSON/ })
    .fill(
      JSON.stringify({
        schema_version: 2,
        guides: [content("パスワードの再設定（改訂）"), content("新しいガイド")],
      }),
    );
  await panel.getByRole("button", { name: "確認" }).click();
  const diff = panel.getByTestId("support-guide-import-diff-0");
  await expect(diff.getByRole("heading", { name: "既存のガイド「パスワードの再設定（改訂）」との違い" })).toBeVisible();
  await expect(diff).toContainText("名前が同じ");
  await expect(diff).toContainText("公開の版 2 と比べています");
  await expect(diff).toContainText("取り込んでも既存のガイドは変わらず、別の下書きとして作ります。");
  await expect(diff).toContainText("追加 1・削除 1・変更 3");
  await expect(diff).toContainText("確認する条件");
  await expect(diff).toContainText("変わった項目: 選択肢、並び順");
  await expect(diff.locator('[data-change-kind="added"]')).toContainText("利用者に知らせる");
  await expect(diff.locator('[data-change-kind="removed"]')).toContainText("本人を確かめる");
  await expect(diff).toContainText("変わった項目: 影響の範囲");
  await expect(panel.getByTestId("support-guide-import-new-1")).toContainText("同じ ID・名前のガイドはありません。");
  await expect(panel.getByRole("button", { name: "取り込む" })).toBeEnabled();
  await expectNoPageOverflow(page);
  await panel.screenshot({ path: testInfo.outputPath("support-guide-import-diff.png") });
});

// #2683: 引き継ぎ先が空のときに継承する窓口（プロファイルの上書き、無ければシステム設定）と、有効な窓口・同じ値の案内。
test("引き継ぎ先は継承する窓口を placeholder で示し、有効な窓口と出どころを出す", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  await openSupportGuides(page);
  await page.getByRole("button", { name: "新しい業務ガイド" }).click();
  const editor = page.getByTestId("support-guide-editor");
  const contact = editor.getByRole("textbox", { name: "引き継ぎ先（窓口の名前）" });
  const effective = editor.getByTestId("support-guide-handoff-contact-effective");
  const same = editor.getByTestId("support-guide-handoff-contact-same");

  // 空ならシステム設定の窓口を継承する（値は欄にコピーしない）。
  await expect(contact).toHaveValue("");
  await expect(contact).toHaveAttribute("placeholder", "未設定（窓口 A を継承）");
  await expect(effective).toHaveText(
    "引き継ぎのときの窓口: 窓口 A（システム設定の窓口を継承）。回答の本文は「担当の窓口」と案内し、回答の注意書きでこの窓口を案内します。",
  );
  await expect(contact).toHaveAccessibleDescription(/引き継ぎのときの窓口: 窓口 A/);
  expect(await findClippedPlaceholders(page)).toEqual([]);
  await expect(same).toHaveCount(0);

  // 継承する窓口と同じ値は、作業を止めずに案内する。
  await contact.fill("窓口 A");
  await expect(same).toHaveText("システム設定と同じ窓口です。空にしても、回答の注意書きで同じ窓口を案内します。");
  await expect(editor.getByRole("button", { name: "保存", exact: true })).toBeEnabled();

  // ガイドの窓口は回答の本文で案内し、注意書きには窓口を出さない。
  await contact.fill("窓口 C");
  await expect(effective).toHaveText(
    "引き継ぎのときの窓口: 窓口 C（この業務ガイド）。回答の本文でこの窓口を案内し、回答の注意書きには窓口を出しません。",
  );
  await expect(same).toHaveCount(0);
  await expectNoPageOverflow(page);
  // 欄（ラベル・入力・補足）だけを残す。
  await contact
    .locator("xpath=ancestor::div[.//p][1]")
    .screenshot({ path: testInfo.outputPath("support-guide-handoff-contact.png") });
});

test("引き継ぎ先はプロファイルの窓口の上書きを継承する", async ({ page }) => {
  await openSupportGuides(page, [], (api) => {
    api.contactOverride = "窓口 B";
  });
  await page.getByRole("button", { name: "新しい業務ガイド" }).click();
  const editor = page.getByTestId("support-guide-editor");
  const contact = editor.getByRole("textbox", { name: "引き継ぎ先（窓口の名前）" });
  await expect(contact).toHaveAttribute("placeholder", "未設定（窓口 B を継承）");
  await expect(editor.getByTestId("support-guide-handoff-contact-effective")).toContainText(
    "窓口 B（プロファイルの窓口の上書きを継承）",
  );
  await contact.fill("窓口 B");
  await expect(editor.getByTestId("support-guide-handoff-contact-same")).toHaveText(
    "プロファイルの窓口の上書きと同じ窓口です。空にしても、回答の注意書きで同じ窓口を案内します。",
  );
});

test("回答の注意書きが無効なら、引き継ぎ先が空のときは窓口の案内なしと示す", async ({ page }) => {
  await openSupportGuides(page, [], (api) => {
    api.inheritedContact = { enabled: false, contact: "" };
    api.contactOverride = "窓口 B";
  });
  await page.getByRole("button", { name: "新しい業務ガイド" }).click();
  const editor = page.getByTestId("support-guide-editor");
  const contact = editor.getByRole("textbox", { name: "引き継ぎ先（窓口の名前）" });
  await expect(contact).toHaveAttribute("placeholder", "未設定（窓口の案内なし）");
  await expect(editor.getByTestId("support-guide-handoff-contact-effective")).toHaveText(
    "引き継ぎのときの窓口: なし。回答の本文は「担当の窓口」と案内します（システム設定で回答の注意書きが無効のため、窓口の案内を出しません）。",
  );
});


test("はい／いいえ条件の分岐値を選んで保存する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const guide = publishedGuide();
  guide.draft = content("合成の条件分岐", {
    conditions: [{id: "checked", label: "確認", type: "boolean", allowed_values: [], required: true, source: "user", unknown_handling: "ask", question: "確認済みですか？", value_aliases: {}}],
    branches: [{id: "b1", when: {condition_id: "checked", operator: "equals", values: ["true"]}, goto_step: "step1", note: ""}],
  });
  const api = await openSupportGuides(page, [guide]);
  await page.getByRole("button", { name: /合成の条件分岐/ }).first().click();
  const editor = page.getByTestId("support-guide-editor");
  const branch = editor.getByRole("listitem", { name: "分岐 1", exact: true });
  const value = branch.getByRole("combobox", {name: /^値/});
  await expect(value).toContainText("はい");
  await value.click();
  await page.getByRole("option", {name: "いいえ", exact: true}).click();
  await editor.getByRole("button", {name: "保存", exact: true}).click();
  const saved = api.requests.find((item) => item.method === "PUT" && item.path.endsWith("/guide-1"));
  expect(saved?.body).toMatchObject({draft: {branches: [{when: {values: ["いいえ"]}}]}});
  await expectNoPageOverflow(page);
  await branch.scrollIntoViewIfNeeded();
  await expect(value).toBeInViewport();
  await page.screenshot({path: testInfo.outputPath("boolean-branch.png")});
});


test("公開版だけを書き出し、アーカイブから復元できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const guide = publishedGuide();
  guide.draft = { ...guide.draft, title: "合成の未公開変更" };
  const api = await openSupportGuides(page, [guide]);
  await page.route("**/api/search-answer-profiles/bv-1/support-guides/export", (route) => route.fulfill({json: envelope({schema_version: 2, guides: api.guides.filter((item) => item.status === "active" && item.published_revision != null).map((item) => item.revisions.find((revision) => revision.revision === item.published_revision)!.content)})}));
  const pending = page.waitForEvent("download");
  await page.getByRole("button", {name: "書き出し", exact: true}).click();
  const download = await pending;
  const data = JSON.parse(await readFile((await download.path())!, "utf8"));
  expect(data.guides).toHaveLength(1);
  expect(data.guides[0].title).toBe(guide.revisions[1].content.title);
  expect(data.guides[0].title).not.toBe(guide.draft.title);
  await page.getByRole("button", {name: /合成の未公開変更/}).first().click();
  const editor = page.getByTestId("support-guide-editor");
  const actions = editor.getByTestId("support-guide-object-actions");
  await actions.getByRole("button", {name: "アーカイブ", exact: true}).click();
  await page.getByRole("alertdialog").getByRole("button", {name: "アーカイブ", exact: true}).click();
  await expect(editor.getByLabel("タイトル")).toBeDisabled();
  await expect(page.getByRole("rowheader", {name: "合成の未公開変更 を編集", exact: true})).toHaveCount(0);
  await actions.getByRole("button", {name: "アーカイブから戻す", exact: true}).click();
  await expect(editor.getByLabel("タイトル")).toBeEnabled();
  await expect(page.getByRole("rowheader", {name: "合成の未公開変更 を編集", exact: true})).toBeVisible();
  await expectNoPageOverflow(page);
  await page.screenshot({path: testInfo.outputPath("restored-guide.png")});
});

// #3060: 外部 Agent の道具の許可と RAG 自身の処理を区別し、既存の保存値を維持する。
test("道具の許可の対象を説明し、外部 Agent 用の選択を保存して再表示する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const guide = publishedGuide();
  guide.draft = content("ガイド A", {
    steps: [{
      id: "step-a", title: "手順 A", purpose: "", depends_on: [], retrieval_hints: [],
      evidence_requirements: [], allowed_tools: ["rag_search", "nl2sql_query"], done_when: "", references: [],
    }],
  });
  const api = await openSupportGuides(page, [guide]);
  await page.getByRole("button", { name: "ガイド A を編集", exact: true }).click();
  const editor = page.getByTestId("support-guide-editor");
  const tools = editor.getByRole("group", { name: "外部 Agent に許可する道具", exact: true });
  await expect(tools).toHaveAccessibleDescription(
    "この業務ガイドを参照する外部 Agent が、手順で使ってよい道具を選びます。RAG 自身の実行範囲は資料の検索・読み取りと回答です。NL2SQL は外部 Agent 側で利用します。"
  );
  await expect(tools.getByRole("checkbox", { name: "データを問い合わせる（NL2SQL）" })).toBeChecked();
  await expect(tools.getByRole("checkbox", { name: "資料を検索する", exact: true })).toBeChecked();
  await tools.scrollIntoViewIfNeeded();
  await expectNoPageOverflow(page);
  await tools.screenshot({ path: testInfo.outputPath("external-agent-tools.png") });

  await tools.getByRole("checkbox", { name: "資料を検索する", exact: true }).uncheck();
  await editor.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => api.guides[0].draft.steps).toMatchObject([{ allowed_tools: ["nl2sql_query"] }]);
  await page.reload();
  await page.getByRole("tab", { name: "業務ガイド" }).click();
  await page.getByRole("button", { name: "ガイド A を編集", exact: true }).click();
  await expect(tools.getByRole("checkbox", { name: "データを問い合わせる（NL2SQL）" })).toBeChecked();
  await expect(tools.getByRole("checkbox", { name: "資料を検索する", exact: true })).not.toBeChecked();
});
