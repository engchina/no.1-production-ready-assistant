import type { Page, Route } from "@playwright/test";

/**
 * 判定の校正（#3578）の API の mock。記入の集合・記入（自分の判定 a1 だけ）・一致と裁定・合成の対照・
 * 校正の job・品質評価の結果の「校正済み」を、合成の内容（合成の手続き）で返す。
 * 記入者 a2 の判定は最初から入っている（食い違いを作る）。人の判定ではない。
 */

type Aspect = "requirement_coverage" | "answer_correctness" | "claim_support";
type Label = "pass" | "fail" | "cannot_judge";
const ASPECTS: Aspect[] = ["requirement_coverage", "answer_correctness", "claim_support"];
const SET_ID = "set0000000000000000000000000001";
const NEW_SET_ID = "set0000000000000000000000000002";
const JOB_ID = "calib000000000000000000000000001";
const MODEL = "judge-model-a";
const PROMPT_SHA = "5f2c0a9e1b7d4c3a8e6f0b2d9c7a1e4f3b5d8c0a2e6f9b1d4c7a0e3f6b9d2c5a";

interface MockItem {
  item_id: string;
  split: "dev" | "holdout";
  question: string;
  answer: string;
  standard_answer: string | null;
  evidence: { id: string; text: string }[];
}

interface StoredLabel {
  label: Label;
  error_type: string | null;
  note: string | null;
}

const TOPICS = ["申請", "変更", "取消", "承認", "更新", "照会"];
const LETTERS = ["A", "B", "C", "D", "E", "F"];

function items(): MockItem[] {
  return TOPICS.map((topic, index) => {
    const days = String(index + 3);
    const name = "合成の手続き " + LETTERS[index] + " の" + topic;
    return {
      item_id: "job-0f3a9c2d:case-0" + String(index + 1),
      split: index % 3 === 0 ? "dev" : "holdout",
      question: name + "の期限は何日ですか？",
      answer: name + "は、受付の日から " + days + " 日以内に行います。\n期限を過ぎた場合は、窓口に理由を添えて届け出ます。",
      standard_answer: "受付の日から " + days + " 日以内。期限を過ぎた場合は理由を添えて届け出る。",
      evidence: [
        { id: "chunk-" + String(index + 1) + "a", text: "第 2 条 " + topic + "は、受付の日から " + days + " 日以内に行う。" },
        { id: "chunk-" + String(index + 1) + "b", text: "第 3 条 期限を過ぎたときは、理由を添えて窓口に届け出る。" },
      ],
    };
  });
}

const ERROR_TYPES = [
  { id: "attribute_swap", group: "誤りの型", title: "対象・属性・比較の相手の取り違え", aspects: "", holdout_target: 5 },
  { id: "general_rule_overreach", group: "誤りの型", title: "一般の規則の当てはめすぎ", aspects: "", holdout_target: 5 },
  { id: "step_order", group: "誤りの型", title: "手順の前後関係の誤り", aspects: "", holdout_target: 5 },
  { id: "scope_expansion", group: "誤りの型", title: "処理の段階・時間の単位の拡大", aspects: "", holdout_target: 5 },
  { id: "unsupported_claim", group: "誤りの型", title: "根拠に無い主張", aspects: "", holdout_target: 5 },
  { id: "partial_omission", group: "誤りの型", title: "必要な論点の一部の不足", aspects: "", holdout_target: 5 },
  { id: "correct_paraphrase", group: "対照", title: "正しい言い換え", aspects: "", holdout_target: 10 },
  { id: "correct_refusal", group: "対照", title: "正しい拒答・利用者への確認", aspects: "", holdout_target: 5 },
];

const CRITERIA: Record<Aspect, string> = {
  requirement_coverage:
    "標準回答が求める論点（結論・手順・条件・例外・数量・期間）を、回答がすべて扱っているか。1 つでも欠ければ fail。",
  answer_correctness:
    "回答の結論と途中の説明が標準回答と根拠に矛盾せず、質問の対象・属性・数量・期間・条件を保ったまま適用しているか。",
  claim_support: "回答の事実の主張が、すべて渡した根拠で裏付けられるか。根拠に無い主張が 1 つでもあれば fail。",
};

const envelope = (data: unknown) => ({ data, error_messages: [], warning_messages: [] });

function key(itemId: string, aspect: string): string {
  return itemId + "|" + aspect;
}

/** a1 と a2 が食い違う行（a2 は fail、a1 は pass）。 */
function disagreed(index: number, aspect: Aspect): boolean {
  return (index === 1 && aspect === "answer_correctness") || (index === 2 && aspect === "claim_support");
}

/** a2 の判定（最初から入っている）。pass と fail が混ざり、2 行だけ a1 と食い違う。 */
function seededOther(source: MockItem[]): Map<string, StoredLabel> {
  const labels = new Map<string, StoredLabel>();
  source.forEach((item, index) => {
    ASPECTS.forEach((aspect, aspectIndex) => {
      const fail = disagreed(index, aspect) || (index + aspectIndex) % 3 === 0;
      labels.set(key(item.item_id, aspect), {
        label: fail ? "fail" : "pass",
        error_type: fail ? "unsupported_claim" : null,
        note: fail ? "根拠に無い断定がある" : null,
      });
    });
  });
  return labels;
}

function kappa(pairs: [Label, Label][]): number | null {
  const judged = pairs.filter(([a, b]) => a !== "cannot_judge" && b !== "cannot_judge");
  if (!judged.length) return null;
  const observed = judged.filter(([a, b]) => a === b).length / judged.length;
  let expected = 0;
  for (const label of ["pass", "fail"] as const) {
    const left = judged.filter(([a]) => a === label).length / judged.length;
    const right = judged.filter(([, b]) => b === label).length / judged.length;
    expected += left * right;
  }
  if (expected >= 1) return null;
  return Math.round(((observed - expected) / (1 - expected)) * 10000) / 10000;
}

export interface JudgeCalibrationMock {
  labels: Map<string, StoredLabel>;
  other: Map<string, StoredLabel>;
  adjudications: Map<string, StoredLabel>;
  calibrated: boolean;
  /** 自分（a1）の未記入の行を埋める（合成の記入。a2 と同じ判定で、2 行だけ食い違う）。 */
  fillMine: () => void;
}

function summary(state: JudgeCalibrationMock, source: MockItem[], name: string, setId: string) {
  const rows = source.length * ASPECTS.length;
  let resolved = 0;
  for (const item of source) {
    for (const aspect of ASPECTS) {
      const k = key(item.item_id, aspect);
      const mine = state.labels.get(k)?.label;
      const other = state.other.get(k)?.label;
      if (state.adjudications.has(k) || (mine && other && mine === other)) resolved += 1;
    }
  }
  return {
    label_set_id: setId,
    name,
    origin: "jobs",
    created_at: "2026-10-20T09:00:00Z",
    human_item_count: source.length,
    synthetic_item_count: 2,
    row_count: rows,
    annotator_count: state.labels.size ? 2 : 1,
    my_labeled: state.labels.size,
    resolved_rows: resolved,
    created_by_me: true,
    latest_calibration: state.calibrated ? calibrationSummary(setId) : null,
  };
}

function progress(source: MockItem[], pick: (k: string) => StoredLabel | undefined) {
  const counts = new Map<string, number>();
  const aspects = ASPECTS.map((aspect) => {
    let labeled = 0;
    let cannot = 0;
    let pass = 0;
    let fail = 0;
    let holdout = 0;
    for (const item of source) {
      if (item.split === "holdout") holdout += 1;
      const label = pick(key(item.item_id, aspect));
      if (!label) continue;
      labeled += 1;
      if (label.label === "cannot_judge") cannot += 1;
      if (item.split === "holdout" && label.label === "pass") pass += 1;
      if (item.split === "holdout" && label.label === "fail") fail += 1;
      if (item.split === "holdout" && label.error_type) {
        counts.set(label.error_type, (counts.get(label.error_type) ?? 0) + 1);
      }
    }
    return {
      aspect,
      rows: source.length,
      labeled,
      cannot_judge: cannot,
      holdout_rows: holdout,
      holdout_pass: pass,
      holdout_fail: fail,
    };
  });
  return {
    aspects,
    error_types: ERROR_TYPES.map((item) => ({ id: item.id, holdout_count: counts.get(item.id) ?? 0 })),
    holdout_target: 50,
    label_target: 20,
  };
}

function split(splitName: string, total: number, kappaValue: number | null, falsePass: number) {
  return {
    split: splitName,
    total,
    judged: total,
    coverage: 1,
    accuracy: kappaValue === null ? null : 0.9,
    kappa: kappaValue,
    false_pass_rate: falsePass,
    false_fail_rate: 0.05,
  };
}

function calibrationSummary(setId: string) {
  return {
    calibration_id: JOB_ID,
    label_set_id: setId,
    status: "SUCCEEDED",
    created_at: "2026-10-21T10:00:00Z",
    finished_at: "2026-10-21T10:12:00Z",
    repeats: 2,
    judge_model_id: MODEL,
    judge_rubric_version: "11",
    prompt_sha256: PROMPT_SHA,
    labels_sha256: "0".repeat(64),
    passed: false,
    error_message: null,
    current: true,
    aspects: [
      {
        aspect: "requirement_coverage",
        passed: false,
        failures: ["κ 0.52 < 0.6"],
        splits: [split("holdout", 56, 0.52, 0.08), split("dev", 24, 0.61, 0.05), split("synthetic", 12, null, 0.17)],
        variance: { runs: 2, items: 92, unstable: 6, unstable_rate: 0.0652 },
      },
      {
        aspect: "answer_correctness",
        passed: false,
        failures: ["誤合格の率 0.14 > 0.1"],
        splits: [split("holdout", 54, 0.63, 0.14), split("dev", 22, 0.7, 0.1), split("synthetic", 16, null, 0.12)],
        variance: { runs: 2, items: 92, unstable: 4, unstable_rate: 0.0435 },
      },
      {
        aspect: "claim_support",
        passed: true,
        failures: [],
        splits: [split("holdout", 61, 0.74, 0.06), split("dev", 25, 0.78, 0.04), split("synthetic", 18, null, 0.05)],
        variance: { runs: 2, items: 104, unstable: 3, unstable_rate: 0.0288 },
      },
    ],
  };
}

function rowStatus(state: JudgeCalibrationMock, k: string) {
  if (state.adjudications.has(k)) return "adjudicated";
  const mine = state.labels.get(k)?.label;
  const other = state.other.get(k)?.label;
  if (!mine || !other) return "pending";
  return mine === other ? "agreed" : "disagreed";
}

function finalLabel(state: JudgeCalibrationMock, k: string): StoredLabel | undefined {
  const adjudicated = state.adjudications.get(k);
  if (adjudicated) return adjudicated;
  const mine = state.labels.get(k);
  const other = state.other.get(k);
  return mine && other && mine.label === other.label ? mine : undefined;
}

/** 判定の校正の API を mock する（`withSet` なら合成の集合が 1 つある状態から始める）。 */
export async function mockJudgeCalibration(
  page: Page,
  options: { syntheticCount?: number; setCount?: number; withSet?: boolean; calibrated?: boolean; canManage?: boolean; evidence?: MockItem["evidence"] } = {}
): Promise<JudgeCalibrationMock> {
  const source = items();
  const evidence = options.evidence;
  if (evidence) source.forEach(item => { item.evidence = evidence; });
  const state: JudgeCalibrationMock = {
    labels: new Map(),
    other: seededOther(source),
    adjudications: new Map(),
    calibrated: options.calibrated ?? false,
    fillMine: () => {
      source.forEach((item, index) => {
        for (const aspect of ASPECTS) {
          const k = key(item.item_id, aspect);
          const other = state.other.get(k)?.label ?? "pass";
          const label: Label = disagreed(index, aspect) ? "pass" : other;
          if (!state.labels.has(k)) state.labels.set(k, { label, error_type: null, note: null });
        }
      });
    },
  };
  const canManage = options.canManage ?? true;
  const sets: { id: string; name: string }[] = options.withSet ? [{ id: SET_ID, name: "校正の集合 A" }] : [];
  for (let i = 1; i < (options.setCount ?? 1); i++) {
    sets.push({ id: `set-extra-${i}`, name: `校正の集合 ${String(i + 1).padStart(2, "0")}` });
  }
  const excluded = new Set<string>();
  let polls = 0;
  let started = false;

  const rows = (aspect: string | null) =>
    source.flatMap((item) =>
      ASPECTS.filter((value) => !aspect || value === aspect).map((value) => ({ item, aspect: value }))
    );

  const detail = (setId: string, name: string) => ({
    summary: summary(state, source, name, setId),
    rubric_id: "answer_quality",
    rubric_version: "1",
    aspects: ASPECTS.map((aspect) => ({
      aspect,
      metric: aspect === "claim_support" ? "claim_support_rate" : aspect,
      criteria: CRITERIA[aspect],
      requires_standard_answer: aspect !== "claim_support",
    })),
    error_types: ERROR_TYPES,
    my_code: state.labels.size ? "a1" : null,
    my_progress: progress(source, (k) => state.labels.get(k)),
    final_progress: canManage ? progress(source, (k) => finalLabel(state, k)) : null,
    can_manage: canManage,
    skipped: {},
    holdout_ratio: 0.7,
  });

  const agreement = (url: URL) => {
    const aspect = url.searchParams.get("aspect");
    const status = url.searchParams.get("status") ?? "disagreed";
    const offset = Number(url.searchParams.get("offset") ?? "0");
    const limit = Number(url.searchParams.get("limit") ?? "25");
    const all = rows(aspect);
    const matched = all.filter((row) => status === "all" || rowStatus(state, key(row.item.item_id, row.aspect)) === status);
    const aspects = ASPECTS.map((value) => {
      const of = rows(value);
      const statuses = of.map((row) => rowStatus(state, key(row.item.item_id, row.aspect)));
      const pairs: [Label, Label][] = [];
      for (const row of of) {
        const k = key(row.item.item_id, row.aspect);
        const mine = state.labels.get(k)?.label;
        const other = state.other.get(k)?.label;
        if (mine && other) pairs.push([mine, other]);
      }
      return {
        aspect: value,
        rows: of.length,
        agreed: statuses.filter((item) => item === "agreed").length,
        disagreed: statuses.filter((item) => item === "disagreed").length,
        adjudicated: statuses.filter((item) => item === "adjudicated").length,
        pending: statuses.filter((item) => item === "pending").length,
        pairs: pairs.length
          ? [
              {
                annotators: ["a1", "a2"],
                compared: pairs.length,
                agreement: pairs.filter(([a, b]) => a === b).length / pairs.length,
                kappa: kappa(pairs),
              },
            ]
          : [],
      };
    });
    return {
      annotators: state.labels.size ? ["a1", "a2"] : ["a2"],
      aspects,
      rows: matched.slice(offset, offset + limit).map((row) => {
        const k = key(row.item.item_id, row.aspect);
        const annotations = [
          ["a1", state.labels.get(k)],
          ["a2", state.other.get(k)],
        ].flatMap(([code, label]) =>
          label && typeof label !== "string" ? [{ code: code as string, ...label }] : []
        );
        const adjudication = state.adjudications.get(k);
        return {
          item_id: row.item.item_id,
          aspect: row.aspect,
          split: row.item.split,
          status: rowStatus(state, k),
          question: row.item.question,
          answer: row.item.answer,
          standard_answer: row.item.standard_answer,
          evidence: row.item.evidence,
          annotations,
          final_label: finalLabel(state, k)?.label ?? null,
          adjudication: adjudication ? { code: "adj1", ...adjudication } : null,
        };
      }),
      total: matched.length,
      offset,
      limit,
    };
  };

  const synthetic = () => {
    const base = [
    {
      item_id: source[1].item_id + "~swap_values",
      base_item_id: source[1].item_id,
      kind: "swap_values",
      description: "数値 4 と 10 を入れ替えた（属性の値の取り違え）",
      fail_aspects: ["answer_correctness", "claim_support"],
      answer: source[1].answer,
      excluded: excluded.has(source[1].item_id + "~swap_values"),
    },
    {
      item_id: source[2].item_id + "~add_unsupported_claim",
      base_item_id: source[2].item_id,
      kind: "add_unsupported_claim",
      description: "根拠に無い断定の文を足した",
      fail_aspects: ["claim_support"],
      answer: source[2].answer,
      excluded: excluded.has(source[2].item_id + "~add_unsupported_claim"),
    },
    ];
    return Array.from({length: options.syntheticCount ?? base.length}, (_, index) => {
      const item = base[index % base.length];
      const itemId = index < base.length ? item.item_id : `${item.item_id}-${index}`;
      return {...item, item_id: itemId, excluded: excluded.has(itemId)};
    });
  };

  const job = () => {
    polls += 1;
    const done = polls > 1;
    if (done) state.calibrated = true;
    return {
      job_id: JOB_ID,
      kind: "calibration",
      status: done ? "SUCCEEDED" : "RUNNING",
      total_cases: 36,
      completed_cases: done ? 36 : 14,
      current_case_id: null,
      current_case_started_at: null,
      time_limit_seconds: 3600,
      error_message: null,
      created_at: "2026-10-21T10:00:00Z",
      started_at: "2026-10-21T10:00:00Z",
      finished_at: done ? "2026-10-21T10:12:00Z" : null,
      heartbeat_at: "2026-10-21T10:05:00Z",
      run_result: null,
    };
  };

  await page.route("**/api/evaluation/jobs/**", async (route: Route) => {
    if (!started || !route.request().url().includes(JOB_ID)) return route.fallback();
    await route.fulfill({ json: envelope(job()) });
  });

  await page.route("**/api/evaluation/calibration/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^.*\/api\/evaluation\/calibration/, "");
    const method = request.method();
    const setMatch = /^\/label-sets\/([^/]+)(\/.*)?$/.exec(path);
    if (path === "/status") {
      return route.fulfill({
        json: envelope({
          judge_model_id: MODEL,
          prompt_sha256: PROMPT_SHA,
          aspects: ASPECTS.map((aspect) => {
            const passed = state.calibrated && aspect === "claim_support";
            return {
              aspect,
              metric: aspect === "claim_support" ? "claim_support_rate" : aspect,
              calibrated: passed,
              kappa: passed ? 0.74 : null,
              calibration_id: passed ? JOB_ID : null,
              calibrated_at: passed ? "2026-10-21T10:12:00Z" : null,
              judge_rubric_version: passed ? "11" : null,
            };
          }),
        }),
      });
    }
    if (path === "/sources") {
      return route.fulfill({
        json: envelope({
          jobs: [
            {
              job_id: "job0f3a9c2d" + "0".repeat(21),
              created_at: "2026-10-18T08:00:00Z",
              finished_at: "2026-10-18T08:40:00Z",
              case_count: 36,
              compared: true,
            },
            {
              job_id: "job7b21e4aa" + "0".repeat(21),
              created_at: "2026-10-15T08:00:00Z",
              finished_at: "2026-10-15T08:30:00Z",
              case_count: 24,
              compared: false,
            },
          ],
        }),
      });
    }
    if (path === "/label-sets" && method === "GET") {
      return route.fulfill({
        json: envelope({
          items: sets.map((item) => summary(state, source, item.name, item.id)),
          can_manage: canManage,
        }),
      });
    }
    if (path === "/label-sets" && method === "POST") {
      const body = request.postDataJSON() as { name: string };
      sets.unshift({ id: NEW_SET_ID, name: body.name });
      return route.fulfill({ status: 201, json: envelope(summary(state, source, body.name, NEW_SET_ID)) });
    }
    if (!setMatch) return route.fulfill({ status: 404, json: { data: null, error_messages: ["not found"] } });
    const setId = setMatch[1] ?? "";
    const sub = setMatch[2] ?? "";
    const set = sets.find((item) => item.id === setId);
    if (!set) return route.fulfill({ status: 404, json: { data: null, error_messages: ["記入の集合が見つかりません。"] } });
    if (sub === "" && method === "GET") return route.fulfill({ json: envelope(detail(set.id, set.name)) });
    if (sub === "" && method === "DELETE") {
      sets.splice(sets.indexOf(set), 1);
      return route.fulfill({ json: envelope({ deleted: true }) });
    }
    if (sub === "/rows") {
      const aspect = url.searchParams.get("aspect");
      const status = url.searchParams.get("status") ?? "all";
      const offset = Number(url.searchParams.get("offset") ?? "0");
      const limit = Number(url.searchParams.get("limit") ?? "1");
      const all = rows(aspect);
      const unlabeled = all.filter((row) => !state.labels.has(key(row.item.item_id, row.aspect)));
      const matched = status === "unlabeled" ? unlabeled : all;
      return route.fulfill({
        json: envelope({
          items: matched.slice(offset, offset + limit).map((row) => {
            const own = state.labels.get(key(row.item.item_id, row.aspect));
            return {
              ...row.item,
              aspect: row.aspect,
              label: own?.label ?? null,
              error_type: own?.error_type ?? null,
              note: own?.note ?? null,
            };
          }),
          total: matched.length,
          offset,
          limit,
          unlabeled: unlabeled.length,
        }),
      });
    }
    if (sub === "/labels" || sub === "/adjudications") {
      const body = request.postDataJSON() as {
        item_id: string;
        aspect: string;
        label: Label | null;
        error_type?: string | null;
        note?: string | null;
      };
      const target = sub === "/labels" ? state.labels : state.adjudications;
      const k = key(body.item_id, body.aspect);
      if (body.label) target.set(k, { label: body.label, error_type: body.error_type ?? null, note: body.note ?? null });
      else target.delete(k);
      if (sub === "/labels") return route.fulfill({ json: envelope(progress(source, (value) => state.labels.get(value))) });
      return route.fulfill({ json: envelope(agreement(url)) });
    }
    if (sub === "/agreement") return route.fulfill({ json: envelope(agreement(url)) });
    if (sub === "/synthetic") return route.fulfill({ json: envelope({ items: synthetic() }) });
    if (sub.startsWith("/synthetic/")) {
      const itemId = decodeURIComponent(sub.slice("/synthetic/".length));
      const body = request.postDataJSON() as { excluded: boolean };
      if (body.excluded) excluded.add(itemId);
      else excluded.delete(itemId);
      return route.fulfill({ json: envelope(synthetic().find((item) => item.item_id === itemId)) });
    }
    if (sub === "/calibrations" && method === "POST") {
      started = true;
      polls = 0;
      return route.fulfill({
        status: 202,
        json: envelope({ job: job(), calibration: { ...calibrationSummary(set.id), status: "RUNNING", passed: null } }),
      });
    }
    if (sub === "/calibrations") {
      return route.fulfill({ json: envelope({ items: state.calibrated ? [calibrationSummary(set.id)] : [] }) });
    }
    return route.fulfill({ status: 404, json: { data: null, error_messages: ["not found"] } });
  });
  return state;
}

/** 記入の行の数（6 項目 × 3 観点）。 */
export const JUDGE_CALIBRATION_ROWS = 18;
