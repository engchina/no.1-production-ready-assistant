import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

test("案内の中のボタンの進行は同じ案内の中に出し、ページの先頭に重ねない（#2034）", () => {
  const ontologyPage = source("../src/features/nl2sql/pages/OntologyBuildPage.tsx");
  const buildSection = source("../src/features/nl2sql/ontology/OntologyBuildSection.tsx");
  const playground = source("../src/features/nl2sql/ontology/OntologyQueryPlayground.tsx");
  assert.match(ontologyPage, /ONTOLOGY_NOTICE_SCHEMA_REFRESH_ORIGINS/u);
  assert.match(ontologyPage, /sharedSchemaRefresh\.jobOrigin/u);
  assert.doesNotMatch(ontologyPage, /onRefreshSchema=/u);
  for (const component of [buildSection, playground]) {
    assert.match(component, /useMutationSchemaSync\(\{[\s\S]*required:/u);
    assert.match(component, /<SchemaSyncNotice/u);
    assert.match(component, /requiredResolved/u);
  }
  // 構築の失敗の警告は案内の中に 1 か所だけ出す。
  assert.match(buildSection, /!schemaScopeFailure \? buildWarningsDisclosure/u);

  const coordinator = source("../src/features/nl2sql/SchemaRefreshCoordinator.tsx");
  assert.match(coordinator, /setJobOrigin\(origin\)/u);

  // 取得の失敗の案内の「再試行」は、押したボタンだけを回し、進行を案内の中に出す。
  const pageNotice = source("../src/components/page-notice.tsx");
  assert.match(pageNotice, /export function RetryPageNotice/u);
  assert.match(pageNotice, /progress=\{progress\}/u);
  for (const path of [
    "../src/features/nl2sql/pages/TableManagementPage.tsx",
    "../src/features/nl2sql/pages/ViewManagementPage.tsx",
    "../src/features/nl2sql/pages/MetadataSqlManagementPage.tsx",
  ]) {
    const page = source(path);
    assert.match(page, /<RetryPageNotice[\s\S]*retrying=\{noticeRetry\.pending\}/u, path);
  }
  const feedback = source("../src/features/nl2sql/pages/FeedbackManagementPage.tsx");
  assert.match(feedback, /if \(origin === "header"\) setMessage\(""\)/u);
  const learning = source("../src/features/nl2sql/pages/QuestionLearningPage.tsx");
  assert.match(learning, /if \(origin === "header"\) setMessage\(""\)/u);

  // サンプルデータの取込の失敗は押した案内の中だけに出す（Toast とページの先頭に重ねない）。
  const workbench = source("../src/features/nl2sql/Nl2SqlWorkbench.tsx");
  const importSample = workbench.slice(
    workbench.indexOf("const importSampleData = useCallback("),
    workbench.indexOf("const sampleImportProgress"),
  );
  assert.doesNotMatch(importSample, /toast\.error|setPageError\(\{ source: "sample-import"/u);
});
