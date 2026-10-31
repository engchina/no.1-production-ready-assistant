import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import test from "node:test";

const workbenchSource = readFileSync(
  new URL("../src/features/nl2sql/Nl2SqlWorkbench.tsx", import.meta.url),
  "utf8"
);
const optionsPanelSource = readFileSync(
  new URL(
    "../src/features/nl2sql/components/Nl2SqlExecutionOptionsPanel.tsx",
    import.meta.url
  ),
  "utf8"
);
const generatedSqlPanelSource = readFileSync(
  new URL("../src/features/nl2sql/components/GeneratedSqlPanel.tsx", import.meta.url),
  "utf8"
);
const logicalStepsListSource = readFileSync(
  new URL("../src/features/nl2sql/components/LogicalStepsList.tsx", import.meta.url),
  "utf8"
);

test("NL2SQL workbench keeps one execution action and removes preview/session buttons", () => {
  assert.equal(workbenchSource.includes('t("nl2sql.action.preview")'), false);
  assert.equal(workbenchSource.includes('t("nl2sql.session.create")'), false);
  assert.equal(workbenchSource.includes('"/api/nl2sql/preview"'), false);
  assert.equal(workbenchSource.includes("previewToJob"), false);
  assert.equal(workbenchSource.includes("QueryOntologyFlow"), false);
  assert.equal(workbenchSource.includes('t("nl2sql.history.count"'), false);
  assert.match(workbenchSource, /<span>\{t\("nl2sql\.action\.run"\)\}<\/span>/);
});

test("execution options default to showing interpretation and show prompt artifacts", () => {
  // 用語・同義語は既定 off(明示 ON のときだけ質問を書き換える)。
  assert.match(
    workbenchSource,
    /const \[rewriteUseGlossary, setRewriteUseGlossary\] = useWorkspaceState\("rewriteUseGlossary", false\);/
  );
  assert.match(
    workbenchSource,
    /const \[useOntologyContext, setUseOntologyContext\] = useWorkspaceState\("useOntologyContext", true\);/
  );
  assert.match(
    workbenchSource,
    /const \[includeInterpretation, setIncludeInterpretation\] = useWorkspaceState\("includeInterpretation", true\);/
  );
  assert.match(
    workbenchSource,
    /const \[includeShowPrompt, setIncludeShowPrompt\] = useWorkspaceState\("includeShowPrompt", false\);/
  );
  assert.match(
    workbenchSource,
    /const \[executionOptionsOpen, setExecutionOptionsOpen\] = useWorkspaceState\("executionOptionsOpen", false\);/
  );
  assert.match(workbenchSource, /use_ontology_context: useOntologyContext/);
  assert.match(workbenchSource, /use_glossary: rewriteUseGlossary/);
  assert.match(workbenchSource, /include_interpretation: includeInterpretation/);
  assert.match(workbenchSource, /include_show_prompt: includeShowPrompt/);
  assert.doesNotMatch(workbenchSource, /rewriteExtraPrompt|setRewriteExtraPrompt|extra_prompt/);
});

test("execution options panel keeps glossary rewrite and removes schema rewrite", () => {
  assert.match(optionsPanelSource, /nl2sql\.rewrite\.useGlossary/);
  assert.doesNotMatch(optionsPanelSource, /nl2sql\.rewrite\.useSchema/);
  assert.doesNotMatch(workbenchSource, /use_schema|rewriteUseSchema/);
  assert.match(optionsPanelSource, /nl2sql\.executionOptions\.useOntology/);
  assert.match(optionsPanelSource, /nl2sql\.executionOptions\.includeInterpretation/);
  assert.match(optionsPanelSource, /nl2sql\.executionOptions\.includeShowPrompt/);
  assert.match(optionsPanelSource, /nl2sql\.executionOptions\.selectAiOverridesUnsupported/);
  assert.match(optionsPanelSource, /aria-expanded=\{open\}/);
  assert.match(optionsPanelSource, /aria-controls="nl2sql-execution-options-body"/);
  assert.match(optionsPanelSource, /engine !== "select_ai"/);
});

test("generated SQL summary renders ontology grounding and show prompt artifact panels", () => {
  assert.match(generatedSqlPanelSource, /data-testid="nl2sql-interpretation-panel"/);
  assert.match(generatedSqlPanelSource, /data-testid="nl2sql-sql-grounding-panel"/);
  // 処理手順パネルは接地確認の下・Show Prompt の上に独立表示する。
  assert.match(generatedSqlPanelSource, /data-testid="nl2sql-logical-steps-panel"/);
  assert.match(generatedSqlPanelSource, /nl2sql\.logicalSteps\.title/);
  // 「Ontology を使う」OFF のとき接地確認を出さない(backend echo で判定)。
  assert.match(generatedSqlPanelSource, /ontology_grounding_enabled !== false/);
  // 公開されたオントロジーが無い job は接地確認をしない。失敗(graphLoadFailed)ではなく情報で出す(#1168)。
  assert.match(generatedSqlPanelSource, /ontology_grounding_skip_reason === "no_published_ontology"/);
  assert.match(generatedSqlPanelSource, /nl2sql\.interpretation\.graphNotPublished/);
  assert.match(
    generatedSqlPanelSource,
    /<InterpretationArtifactPanel[\s\S]*?\/>\s*<SqlLogicalStepsPanel[\s\S]*?\/>\s*<ShowPromptArtifactPanel/
  );
  assert.doesNotMatch(generatedSqlPanelSource, /useProfileOntologyView/);
  assert.match(generatedSqlPanelSource, /artifact\.ontology_graph/);
  assert.doesNotMatch(generatedSqlPanelSource, /nl2sql\.interpretation\.inputTitle/);
  assert.doesNotMatch(generatedSqlPanelSource, /nl2sql\.interpretation\.sqlTitle/);
  assert.match(generatedSqlPanelSource, /data-testid="nl2sql-show-prompt-panel"/);
  assert.match(generatedSqlPanelSource, /artifact\.prompt/);
  assert.match(generatedSqlPanelSource, /whitespace-pre-wrap/);
});

test("処理手順は業務者向け説明と技術詳細を併記し、旧文字列手順へ縮退できる", () => {
  // 業務行(business)と技術行(technical)の 2 行構造。
  assert.match(logicalStepsListSource, /step\.business/);
  assert.match(logicalStepsListSource, /step\.technical/);
  assert.match(logicalStepsListSource, /nl2sql\.logicalSteps\.technicalLabel/);
  assert.match(logicalStepsListSource, /nl2sql\.logicalSteps\.technicalSrLabel/);
  // details が無い過去 job / 旧 API では従来の文字列手順を業務行として描画する。
  assert.match(logicalStepsListSource, /fallbackSteps/);
  // 生成 SQL パネルは自前の <ol> を持たず共有コンポーネントへ委譲する。
  assert.match(generatedSqlPanelSource, /<LogicalStepsList/);
  assert.match(generatedSqlPanelSource, /logical_step_details/);
});
