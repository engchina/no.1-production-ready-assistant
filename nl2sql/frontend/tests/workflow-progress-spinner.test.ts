import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { activitySpinnerTarget } from "../src/features/nl2sql/components/workflowProgressSpinner";

const steps = (...statuses: string[]) => statuses.map((status) => ({ status }));

test("job の進行状況の帯は、実行中の先頭の工程でだけスピナーを回す（#416）", () => {
  assert.equal(
    activitySpinnerTarget({ active: true, collapsed: false, steps: steps("done", "running", "pending") }),
    1
  );
  // 並行して複数の工程が実行中でも、スピナーは 1 つ（先頭の工程）。
  assert.equal(
    activitySpinnerTarget({ active: true, collapsed: false, steps: steps("done", "running", "running") }),
    1
  );
});

test("実行中の工程が見えないときは、見出しのアイコンで 1 つ回す（#416）", () => {
  // 待機中（どの工程もまだ始まっていない）。
  assert.equal(
    activitySpinnerTarget({ active: true, collapsed: false, steps: steps("pending", "pending") }),
    "header"
  );
  // 工程の切り替わりの間（前の工程が終わり、次の工程がまだ始まっていない）。
  assert.equal(
    activitySpinnerTarget({ active: true, collapsed: false, steps: steps("done", "pending") }),
    "header"
  );
  // 工程を畳んでいる。
  assert.equal(
    activitySpinnerTarget({ active: true, collapsed: true, steps: steps("done", "running") }),
    "header"
  );
});

test("job が進んでいなければスピナーを回さない（#416）", () => {
  assert.equal(
    activitySpinnerTarget({ active: false, collapsed: false, steps: steps("done", "running") }),
    null
  );
  assert.equal(activitySpinnerTarget({ active: false, collapsed: true, steps: steps("error") }), null);
});

test("job の開始ボタンは送信中だけ loading にし、job の間のスピナーは進行状況の帯に任せる（#416）", () => {
  const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
  const workbench = read("../src/features/nl2sql/Nl2SqlWorkbench.tsx");
  const build = read("../src/features/nl2sql/ontology/OntologyBuildSection.tsx");
  const strip = read("../src/features/nl2sql/components/WorkflowProgressStrip.tsx");

  assert.match(workbench, /loading=\{submitting\}\s*disabled=\{!question\.trim\(\) \|\| active/u);
  assert.doesNotMatch(workbench, /loading=\{jobActive\}/u);
  assert.match(build, /loading=\{busy === "start"\}\s*disabled=\{busy !== "" \|\| jobRunning/u);
  assert.doesNotMatch(build, /loading=\{busy === "start" \|\| jobRunning\}/u);
  // 帯の中のスピナーは activitySpinnerTarget の 1 か所だけが決める。
  assert.match(strip, /spinnerTarget === "header" \? <Spinner size=\{20\} \/>/u);
  assert.match(strip, /spinning=\{spinnerTarget === index\}/u);
  assert.equal([...strip.matchAll(/<Spinner /gu)].length, 2);
});

test("「中止」の要求中は、進行状況の帯のスピナーを止めて中止のボタンの 1 つにする（#416）", () => {
  const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
  const strip = read("../src/features/nl2sql/components/WorkflowProgressStrip.tsx");
  const operation = read("../src/features/nl2sql/components/OperationStatusStrip.tsx");
  const build = read("../src/features/nl2sql/ontology/OntologyBuildSection.tsx");

  assert.match(strip, /active: active && activityIcon === "spinner"/u);
  assert.match(operation, /activityIndicator=\{!cancelRequesting\}/u);
  assert.match(operation, /loading=\{cancelRequesting\}/u);
  assert.match(build, /activityIcon=\{busy === "cancel" \? "none" : "spinner"\}/u);
  assert.match(build, /loading=\{busy === "cancel"\}/u);
});
