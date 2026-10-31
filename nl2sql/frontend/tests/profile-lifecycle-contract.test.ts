import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const profilePage = readFileSync(
  new URL("../src/features/nl2sql/pages/ProfileManagementPage.tsx", import.meta.url),
  "utf8",
);
const profileSaveProgress = readFileSync(
  new URL("../src/features/nl2sql/components/ProfileSaveProgress.tsx", import.meta.url),
  "utf8",
);
const profileSyncPresentation = readFileSync(
  new URL("../src/features/nl2sql/profileSyncPresentation.ts", import.meta.url),
  "utf8",
);
const ontologyPage = readFileSync(
  new URL("../src/features/nl2sql/pages/OntologyBuildPage.tsx", import.meta.url),
  "utf8",
);
const ontologyBuildSection = readFileSync(
  new URL("../src/features/nl2sql/ontology/OntologyBuildSection.tsx", import.meta.url),
  "utf8",
);
const ontologyQueryPlayground = readFileSync(
  new URL("../src/features/nl2sql/ontology/OntologyQueryPlayground.tsx", import.meta.url),
  "utf8",
);
const fileDropzone = readFileSync(
  new URL("../src/components/ui/file-dropzone.tsx", import.meta.url),
  "utf8",
);
const clearActionButton = readFileSync(
  new URL("../../../platform/packages/ui/src/components/ui/clear-action-button.tsx", import.meta.url),
  "utf8",
);
const messages = readFileSync(new URL("../src/lib/i18n.ts", import.meta.url), "utf8");

test("profile save queues Oracle sync without touching Ontology or detail refresh", () => {
  assert.match(profilePage, /\/oracle-sync-jobs/u);
  assert.match(profilePage, /"Idempotency-Key"/u);
  assert.match(profilePage, /BUSINESS_SELECT_AI_DB_PROFILES_URL/u);
  assert.doesNotMatch(profilePage, /BUSINESS_SELECT_AI_DB_PROFILES_DETAIL_URL/u);
  assert.doesNotMatch(profilePage, /\/ontology-view/u);
  assert.doesNotMatch(profilePage, /\/select-ai-profile/u);
  assert.doesNotMatch(profilePage, /select-ai-agent\/assets\/refresh/u);
});

test("profile Oracle sync exposes progress, failure recovery and retry", () => {
  assert.match(profilePage, /refetchInterval/u);
  assert.match(profilePage, /<ProfileSaveProgress/u);
  assert.match(profileSaveProgress, /<WorkflowProgressStrip/u);
  assert.match(profileSaveProgress, /role=\{presentation\.active \? "status" : undefined\}/u);
  assert.match(profileSaveProgress, /severity="danger"/u);
  assert.match(profileSaveProgress, /testId: `profile-save-step-\$\{step\.id\}`/u);
  assert.match(profileSyncPresentation, /submission_failed/u);
  assert.match(profileSyncPresentation, /job\.oracle_result/u);
  assert.match(profileSyncPresentation, /job\.agent_result/u);
  assert.match(profilePage, /\/oracle-sync-jobs\/\$\{oracleSyncJob\.job_id\}\/retry/u);
  assert.match(messages, /業務プロファイルは保存されましたが、Oracle 反映に失敗しました/u);
  assert.doesNotMatch(profilePage, /function OracleMutationResult/u);
  assert.doesNotMatch(profilePage, /profile-oracle-result/u);
  assert.doesNotMatch(messages, /Oracle Profile 反映結果/u);
});

test("Ontology page uses AI build as the only user-facing build action", () => {
  assert.doesNotMatch(ontologyPage, /\/ontology-view\/materialize/u);
  assert.doesNotMatch(ontologyPage, /ontologyViewStale/u);
  assert.doesNotMatch(ontologyPage, /data-testid="ontology-view-lifecycle"/u);
  assert.match(ontologyPage, /OntologyBuildSection/u);
  assert.match(ontologyBuildSection, /ontology-build-markdown/u);
});

test("Ontology publish refreshes the grounding graph without a Mermaid UI export", () => {
  assert.match(ontologyPage, /await refreshOntologyView\(\);/u);
  assert.match(ontologyPage, /onPublished=\{handleOntologyPublished\}/u);
  assert.match(ontologyPage, /<OntologyQueryPlayground/u);
  assert.doesNotMatch(ontologyPage, /OntologyMermaidPanel/u);
  assert.doesNotMatch(ontologyPage, /mermaidRefreshToken/u);
  assert.match(ontologyBuildSection, /onPublished\?: \(\) => void \| Promise<void>/u);
  assert.match(ontologyBuildSection, /void onPublished\?\.\(\)/u);
});

test("Ontology workspace is loaded only by the explicit fetch action", () => {
  assert.match(ontologyPage, /const workspaceRequested =/u);
  assert.match(
    ontologyPage,
    /useProfileDetail\(workspaceRequested \? selectedProfileId : ""\)/u
  );
  assert.match(ontologyPage, /useProfileOntologyView\(selectedProfileId, workspaceRequested\)/u);
  assert.doesNotMatch(ontologyPage, /useProfileDetail\(selectedProfileId\)/u);
  assert.match(ontologyPage, /id="ontology-workspace-not-loaded"/u);
  assert.match(ontologyPage, /testId="ontology-workspace-loading"/u);
  assert.match(ontologyPage, /data-testid="ontology-view-fetch"/u);
  assert.match(ontologyPage, /setOntologyViewRequestedProfileId\(""\)/u);
  assert.match(ontologyQueryPlayground, /loadState === "not_loaded"/u);
  assert.match(messages, /オントロジー情報は未取得です/u);
});

test("Grounding graph tracks revision changes and supports reset", () => {
  assert.match(ontologyQueryPlayground, /graphRevisionId/u);
  assert.match(ontologyQueryPlayground, /resetGroundingState/u);
  assert.match(ontologyQueryPlayground, /ontology-playground-clear/u);
  assert.match(messages, /質問・確認結果をクリア/u);
  assert.doesNotMatch(messages, /SQL 生成用 Mermaid ER 技術表現/u);
});

test("Ontology node picker uses the shared bounded list standard", () => {
  assert.match(ontologyQueryPlayground, /ONTOLOGY_NODE_PICKER_MAX_ITEMS = 12/u);
  assert.match(ontologyQueryPlayground, /INFORMATION_LIST_SCROLL_CLASS/u);
  assert.match(ontologyQueryPlayground, /INFORMATION_LIST_ROW_CLASS/u);
  assert.match(ontologyQueryPlayground, /INFORMATION_TABLE_FOCUS_CLASS/u);
  assert.match(ontologyQueryPlayground, /data-testid="ontology-inspector-node-picker-scroll-region"/u);
  assert.match(ontologyQueryPlayground, /role="region"/u);
});

test("Grounding graph and file dropzones share the clear button implementation", () => {
  assert.match(clearActionButton, /export function ClearActionButton/u);
  assert.match(clearActionButton, /variant="secondary"/u);
  assert.match(clearActionButton, /size = "sm"/u);
  // 同じ行の部品とは size でそろえる（マウス環境で 44px にしない。#613）。
  assert.doesNotMatch(clearActionButton, /matchButtonHeight|touchTarget/u);
  assert.match(clearActionButton, /"whitespace-nowrap"/u);
  assert.match(fileDropzone, /<ClearActionButton/u);
  assert.match(ontologyQueryPlayground, /<ClearActionButton/u);
  assert.doesNotMatch(ontologyQueryPlayground, /variant="ghost"[\s\S]*ontology-playground-clear/u);
});

test("Grounding graph remains the single user-facing ontology graph", () => {
  assert.match(ontologyPage, /<OntologyQueryPlayground/u);
  assert.match(ontologyQueryPlayground, /OntologyGraphCanvas/u);
  assert.match(ontologyQueryPlayground, /OntologyErDetailsPanel/u);
  assert.match(ontologyQueryPlayground, /graphViewMode/u);
  assert.match(messages, /質問のオントロジー接地確認用グラフ/u);
  assert.match(messages, /物理 ER/u);
  assert.match(messages, /グラフを拡大/u);
  assert.match(messages, /グラフを縮小/u);
  assert.match(messages, /グラフ全体を表示/u);
});

test("all profiles share the physical delete flow and retained-state confirmation", () => {
  assert.match(
    messages,
    /そのすべてのオントロジー範囲設定、Oracle DBMS_CLOUD_AI Profile、Select AI Agent 関連アセットを完全に削除/u
  );
  assert.match(messages, /監査履歴は削除されません/u);
  assert.doesNotMatch(profilePage, /profile\.id !== "default"/u);
  assert.doesNotMatch(profilePage, /if \(profile\.id === "default"\) return;/u);
  assert.match(profilePage, /\/api\/nl2sql\/profiles\/\$\{encodeURIComponent\(profile\.id\)\}/u);
});
