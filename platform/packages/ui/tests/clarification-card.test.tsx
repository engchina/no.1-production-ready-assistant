// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ClarificationCard,
  ClarificationRecord,
  DEFAULT_CLARIFICATION_LABELS,
  clarificationLabels,
  clarificationRecordAnswerText,
  type ClarificationCardProps,
  type ClarificationItem,
} from "../src";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

const period: ClarificationItem = {
  id: "period",
  prompt: "どの期間の売上を見ますか？",
  reason: "「最近」の期間が質問から決められません。",
  options: [
    { id: "month", label: "今月", description: "2026 年 10 月 1 日から今日まで" },
    { id: "quarter", label: "今四半期", detail: <span data-testid="product-detail">SALES.ORDER_DATE</span> },
  ],
};
const regions: ClarificationItem = {
  id: "regions",
  prompt: "どの地域を含めますか？",
  selection: "multiple",
  options: [
    { id: "east", label: "東日本" },
    { id: "west", label: "西日本" },
  ],
};

// 選択肢の無い（自由入力だけで答える）確認事項（#3366）。
const freeOnly: ClarificationItem = { id: "unit", prompt: "金額の単位は？", options: [] };

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function render(props: Partial<ClarificationCardProps> = {}) {
  const onConfirm = props.onConfirm ?? vi.fn();
  act(() => root.render(<ClarificationCard items={[period]} onConfirm={onConfirm} {...props} />));
  return onConfirm;
}

function byTestId<T extends HTMLElement = HTMLElement>(id: string): T {
  const element = host.querySelector<T>(`[data-testid="${id}"]`);
  if (!element) throw new Error(`not found: ${id}`);
  return element;
}

function optionInput(itemId: string, optionId: string): HTMLInputElement {
  const input = host.querySelector<HTMLInputElement>(
    `[data-item-id="${itemId}"] [data-option-id="${optionId}"] input`
  );
  if (!input) throw new Error(`option not found: ${itemId}/${optionId}`);
  return input;
}

function otherField(itemId: string): HTMLTextAreaElement {
  const textarea = host.querySelector<HTMLTextAreaElement>(`[data-item-id="${itemId}"] textarea`);
  if (!textarea) throw new Error(`other not found: ${itemId}`);
  return textarea;
}

function type(element: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  act(() => {
    setter.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function click(element: HTMLElement) {
  act(() => element.click());
}

describe("ClarificationCard", () => {
  it("題・確認事項・確認する理由・選択肢・その他（自由入力）を出す", () => {
    render();
    const card = byTestId("clarification-card");
    expect(card.getAttribute("aria-labelledby")).toBeTruthy();
    expect(card.querySelector("h3")?.textContent).toContain("回答の前に確認させてください");
    const fieldset = card.querySelector("fieldset")!;
    expect(document.getElementById(fieldset.getAttribute("aria-labelledby")!)?.textContent).toBe(period.prompt);
    expect(document.getElementById(fieldset.getAttribute("aria-describedby")!)?.textContent).toContain(
      "確認する理由"
    );
    expect(optionInput("period", "month").type).toBe("radio");
    expect(card.textContent).toContain("2026 年 10 月 1 日から今日まで");
    expect(byTestId("product-detail").textContent).toBe("SALES.ORDER_DATE");
    // 「その他（自由入力）」は選択肢があっても常に出す。
    expect(card.querySelector(`label[for="${otherField("period").id}"]`)?.textContent).toContain("その他（自由入力）");
  });

  it("答えるまで確定を押せず、押せない理由を aria-describedby で結ぶ", () => {
    render();
    const confirm = byTestId<HTMLButtonElement>("clarification-card-confirm");
    expect(confirm.disabled).toBe(true);
    const blocked = byTestId("clarification-card-blocked");
    expect(confirm.getAttribute("aria-describedby")).toBe(blocked.id);
    expect(blocked.textContent).toBe(DEFAULT_CLARIFICATION_LABELS.confirmBlocked);

    click(optionInput("period", "month"));
    expect(confirm.disabled).toBe(false);
    expect(confirm.getAttribute("aria-describedby")).toBeNull();
    expect(host.querySelector('[data-testid="clarification-card-blocked"]')).toBeNull();
  });

  it("1 つだけ選ぶ確認事項では、選択肢と自由入力はどちらか一方", () => {
    const onConfirm = render();
    click(optionInput("period", "month"));
    type(otherField("period"), "  先月の 15 日から  ");
    expect(optionInput("period", "month").checked).toBe(false);
    click(byTestId("clarification-card-confirm"));
    expect(onConfirm).toHaveBeenLastCalledWith([
      { itemId: "period", optionIds: [], otherText: "先月の 15 日から" },
    ]);

    click(optionInput("period", "quarter"));
    expect(otherField("period").value).toBe("");
    click(byTestId("clarification-card-confirm"));
    expect(onConfirm).toHaveBeenLastCalledWith([{ itemId: "period", optionIds: ["quarter"], otherText: "" }]);
  });

  it("空白だけの自由入力は答えにしない", () => {
    render();
    type(otherField("period"), "   ");
    expect(byTestId<HTMLButtonElement>("clarification-card-confirm").disabled).toBe(true);
  });

  it("複数選ぶ確認事項はチェックボックスで、自由入力と併せて答えられる", () => {
    const onConfirm = render({ items: [regions] });
    expect(optionInput("regions", "east").type).toBe("checkbox");
    expect(host.textContent).toContain(DEFAULT_CLARIFICATION_LABELS.multipleHint);
    click(optionInput("regions", "east"));
    click(optionInput("regions", "west"));
    click(optionInput("regions", "east"));
    type(otherField("regions"), "沖縄");
    click(byTestId("clarification-card-confirm"));
    expect(onConfirm).toHaveBeenLastCalledWith([{ itemId: "regions", optionIds: ["west"], otherText: "沖縄" }]);
  });

  it("選択肢の無い確認事項は、自由入力だけの言い方にする（「その他」「選択肢」と言わない）", () => {
    const onConfirm = render({ items: [freeOnly] });
    const card = byTestId("clarification-card");
    const field = otherField("unit");
    expect(card.querySelector(`label[for="${field.id}"]`)?.textContent).toContain("答え");
    expect(field.placeholder).toBe("答えを書いてください");
    expect(byTestId("clarification-card-blocked").textContent).toBe("答えを書くと押せます。");
    expect(card.textContent).not.toContain("その他");
    expect(card.textContent).not.toContain("選択肢");
    type(field, "千円");
    click(byTestId("clarification-card-confirm"));
    expect(onConfirm).toHaveBeenLastCalledWith([{ itemId: "unit", optionIds: [], otherText: "千円" }]);
  });

  it("複数の確認事項の押せない理由は、答えていない確認事項の答え方で言い方を変える", () => {
    render({ items: [freeOnly, { ...freeOnly, id: "scale", prompt: "桁は？" }] });
    expect(byTestId("clarification-card-blocked").textContent).toBe(
      "すべての確認事項で答えを書くと押せます（残り 2 件）。"
    );
    act(() => root.render(<ClarificationCard items={[period, freeOnly]} onConfirm={vi.fn()} />));
    expect(byTestId("clarification-card-blocked").textContent).toBe("すべての確認事項に答えると押せます（残り 2 件）。");
    // 選択肢のある確認事項の欄は「その他（自由入力）」のまま。
    expect(host.querySelector(`label[for="${otherField("period").id}"]`)?.textContent).toContain("その他（自由入力）");
    click(optionInput("period", "month"));
    expect(byTestId("clarification-card-blocked").textContent).toBe(
      "すべての確認事項で答えを書くと押せます（残り 1 件）。"
    );
  });

  it("複数の確認事項は、すべてに答えると確定を押せ、残りの件数を理由に出す", () => {
    render({ items: [period, regions] });
    expect(byTestId("clarification-card-meta").textContent).toBe("確認事項 2 件");
    expect(byTestId("clarification-card-blocked").textContent).toContain("残り 2 件");
    click(optionInput("period", "month"));
    expect(byTestId("clarification-card-blocked").textContent).toContain("残り 1 件");
    click(optionInput("regions", "west"));
    expect(byTestId<HTMLButtonElement>("clarification-card-confirm").disabled).toBe(false);
  });

  it("スキップは渡したときだけ出して補足を説明として結び、「確認をやめる」は置かない", () => {
    const onSkip = vi.fn();
    render();
    expect(host.querySelector('[data-testid="clarification-card-skip"]')).toBeNull();

    render({ onSkip });
    const skip = byTestId<HTMLButtonElement>("clarification-card-skip");
    expect(skip.textContent).toBe("確認せずに回答");
    expect(document.getElementById(skip.getAttribute("aria-describedby")!)?.textContent).toContain(
      "前提を示して回答します。"
    );
    // 確認を終えるのは「確認せずに回答」だけ（質問を取り消すのは入力欄の「停止」。#2311）。
    expect(host.textContent).not.toContain("確認をやめる");
    expect(host.textContent).not.toContain("質問は入力欄に戻ります。");
    expect(Array.from(host.querySelectorAll("button")).map((button) => button.textContent)).toEqual([
      "この内容で回答",
      "確認せずに回答",
    ]);
    // 答える前でもスキップは押せる。
    click(skip);
    expect(onSkip).toHaveBeenCalledTimes(1);
  });

  it("確定の処理中はフォームを答えた内容の記録と経過の行 1 つに置き換える", () => {
    const onConfirm = vi.fn();
    const onSkipWhileConfirming = vi.fn();
    const items = [period, regions, { ...regions, id: "extra", prompt: "任意の確認", optional: true }];
    const value = {
      period: { optionIds: ["month"], otherText: "" },
      regions: { optionIds: ["east", "west"], otherText: "沖縄" },
    };
    render({ items, value, onValueChange: vi.fn(), onConfirm, onSkip: vi.fn(), error: <p data-testid="err">失敗</p> });
    const confirm = byTestId<HTMLButtonElement>("clarification-card-confirm");
    act(() => confirm.focus());
    render({
      items,
      value,
      onValueChange: vi.fn(),
      onConfirm,
      onSkip: vi.fn(),
      onSkipWhileConfirming,
      busyAction: "confirm",
      round: { current: 2, max: 2 },
    });
    // フォーム（選択肢・自由入力・操作の行・押せない理由・最後の確認の補足）は出さない。
    expect(host.querySelector("fieldset")).toBeNull();
    expect(host.querySelector("textarea")).toBeNull();
    expect(host.querySelector('[data-testid="clarification-card-confirm"]')).toBeNull();
    expect(host.querySelector('[data-testid="clarification-card-skip"]')).toBeNull();
    expect(host.querySelector('[data-testid="clarification-card-last-round"]')).toBeNull();
    // 答えた内容の記録（答えの無い任意の確認事項は出さない）。
    const answered = Array.from(host.querySelectorAll('[data-testid="clarification-card-answered-entry"]')).map(
      (entry) => [entry.querySelector("dt")?.textContent, entry.querySelector("dd")?.textContent]
    );
    expect(answered).toEqual([
      [period.prompt, "今月"],
      [regions.prompt, "東日本、西日本、その他: 沖縄"],
    ]);
    // 経過の行は 1 つで、スピナーもカードの中に 1 つだけ。待たずに進める「確認せずに回答」を経過の行に出す。
    const progress = byTestId("clarification-card-progress");
    expect(progress.textContent).toContain(DEFAULT_CLARIFICATION_LABELS.confirming);
    expect(byTestId("clarification-card-progress-timer").textContent).toContain("00:00");
    expect(host.querySelectorAll(".pr-spinner")).toHaveLength(1);
    const skipWhileConfirming = Array.from(progress.querySelectorAll("button")).find(
      (button) => button.textContent === "確認せずに回答"
    )!;
    click(skipWhileConfirming);
    expect(onSkipWhileConfirming).toHaveBeenCalledTimes(1);
    // ボタンが消えたので、フォーカスは題へ移す。
    expect(document.activeElement).toBe(host.querySelector("h3"));
    expect(onConfirm).not.toHaveBeenCalled();

    // 失敗したらフォームと入力が戻り、失敗を操作の行の直上に出し、確認事項の文へフォーカスを移す。
    render({ items, value, onValueChange: vi.fn(), onConfirm, onSkip: vi.fn(), error: <p data-testid="err">失敗</p> });
    expect(optionInput("period", "month").checked).toBe(true);
    expect(otherField("regions").value).toBe("沖縄");
    expect(byTestId("err").nextElementSibling?.querySelector('[data-testid="clarification-card-confirm"]')).toBeTruthy();
    expect(document.activeElement?.textContent).toBe(period.prompt);
  });

  it("経過の行の「確認せずに回答」は渡したときだけ出す", () => {
    render({ onSkip: vi.fn(), busyAction: "confirm", defaultValue: { period: { optionIds: ["month"], otherText: "" } } });
    expect(byTestId("clarification-card-progress").querySelector("button")).toBeNull();
  });

  it("スキップの処理中は押したボタンだけを loading にし、ほかの操作と入力を押せなくする", () => {
    const onConfirm = vi.fn();
    render({
      onConfirm,
      onSkip: vi.fn(),
      dismissAction: { label: "直すのをやめる", onClick: vi.fn() },
      defaultValue: { period: { optionIds: ["month"], otherText: "" } },
    });
    render({ onConfirm, onSkip: vi.fn(), dismissAction: { label: "直すのをやめる", onClick: vi.fn() }, busyAction: "skip" });
    const skip = byTestId<HTMLButtonElement>("clarification-card-skip");
    expect(skip.getAttribute("aria-busy")).toBe("true");
    expect(byTestId<HTMLButtonElement>("clarification-card-confirm").disabled).toBe(true);
    expect(byTestId<HTMLButtonElement>("clarification-card-dismiss").disabled).toBe(true);
    expect(optionInput("period", "month").disabled).toBe(true);
    expect(otherField("period").disabled).toBe(true);
    // 選んだ内容は処理中も残る。
    expect(optionInput("period", "month").checked).toBe(true);
    click(byTestId("clarification-card-confirm"));
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("位置と往復を出し、上限の回では最後の確認であることを添える", () => {
    render({ progress: { current: 2, total: 3 }, round: { current: 1, max: 2 } });
    expect(byTestId("clarification-card-meta").textContent).toBe("確認事項 2 / 3・確認 1 回目（上限 2 回）");
    expect(host.querySelector('[data-testid="clarification-card-last-round"]')).toBeNull();
    render({ progress: { current: 2, total: 3 }, round: { current: 2, max: 2 } });
    expect(byTestId("clarification-card-last-round").textContent).toContain("これが最後の確認です。");
  });

  it("確認事項が変わったら答えを空にし、確認事項の文（題の 1 段下の見出し）へフォーカスを移す", () => {
    render();
    click(optionInput("period", "month"));
    const next: ClarificationItem = { ...regions, id: "regions-2" };
    act(() => root.render(<ClarificationCard items={[next]} onConfirm={vi.fn()} />));
    expect(optionInput("regions-2", "east").checked).toBe(false);
    expect(document.activeElement).toBe(host.querySelector("h4"));
    expect(host.querySelector("h4")?.classList.contains("pr-clarification-heading")).toBe(true);
    expect(host.querySelector("h3")?.classList.contains("pr-clarification-heading")).toBe(true);
    expect(document.activeElement?.textContent).toBe(regions.prompt);
  });

  it("autoFocus がなければ最初に出たときはフォーカスを動かさない", () => {
    render();
    expect(document.activeElement).not.toBe(host.querySelector("h4"));
    act(() => root.unmount());
    root = createRoot(host);
    render({ autoFocus: true });
    expect(document.activeElement).toBe(host.querySelector("h4"));
    expect(host.querySelector("h4")?.classList.contains("pr-clarification-heading")).toBe(true);
    expect(host.querySelector("h3")?.classList.contains("pr-clarification-heading")).toBe(true);
  });

  it("受控の答えを使い、変更を onValueChange に渡す", () => {
    const onValueChange = vi.fn();
    render({ value: { period: { optionIds: ["quarter"], otherText: "" } }, onValueChange });
    expect(optionInput("period", "quarter").checked).toBe(true);
    click(optionInput("period", "month"));
    expect(onValueChange).toHaveBeenLastCalledWith({ period: { optionIds: ["month"], otherText: "" } });
    // 受控なので親が値を変えるまで表示は変わらない。
    expect(optionInput("period", "quarter").checked).toBe(true);
  });

  it("製品が決めた押せない理由を優先する", () => {
    render({ blockedReason: "直しを送ってから生成してください。", defaultValue: { period: { optionIds: ["month"], otherText: "" } } });
    const confirm = byTestId<HTMLButtonElement>("clarification-card-confirm");
    expect(confirm.disabled).toBe(true);
    expect(document.getElementById(confirm.getAttribute("aria-describedby")!)?.textContent).toBe(
      "直しを送ってから生成してください。"
    );
  });

  it("答えなくてよい確認事項は押せない理由に数えない", () => {
    const onConfirm = render({ items: [{ ...period, optional: true }] });
    const confirm = byTestId<HTMLButtonElement>("clarification-card-confirm");
    expect(confirm.disabled).toBe(false);
    click(confirm);
    expect(onConfirm).toHaveBeenLastCalledWith([{ itemId: "period", optionIds: [], otherText: "" }]);
    render({ items: [{ ...period, optional: true }, regions] });
    expect(byTestId("clarification-card-blocked").textContent).toContain("残り 1 件");
  });

  it("製品の操作を確定の右に、入力の取りやめを反対の端に置く", () => {
    const onDismiss = vi.fn();
    render({
      onSkip: vi.fn(),
      actions: <button type="button" data-testid="product-action">直しを伝える</button>,
      dismissAction: { label: "直すのをやめる", onClick: onDismiss },
    });
    const buttons = Array.from(host.querySelectorAll("button")).map((button) => button.textContent);
    expect(buttons).toEqual(["この内容で回答", "直しを伝える", "確認せずに回答", "直すのをやめる"]);
    const dismiss = byTestId<HTMLButtonElement>("clarification-card-dismiss");
    expect(dismiss.getAttribute("aria-describedby")).toBeNull();
    click(dismiss);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("製品の名詞で文言を組む（NL2SQL）", () => {
    const labels = clarificationLabels({ stage: "SQL の生成", action: "SQL を生成" });
    expect(labels.title).toBe("SQL の生成の前に確認させてください");
    expect(labels.confirm).toBe("この内容で SQL を生成");
    expect(labels.skip).toBe("確認せずに SQL を生成");
    expect(labels.skipHint).toBe("前提を示して SQL を生成します。");
    expect(labels.recordTitle).toBe("SQL の生成前に確認した内容");
    expect(DEFAULT_CLARIFICATION_LABELS.confirm).toBe("この内容で回答");
    expect(DEFAULT_CLARIFICATION_LABELS.recordTitle).toBe("回答前に確認した内容");
    expect(DEFAULT_CLARIFICATION_LABELS).not.toHaveProperty("cancel");
  });
});

describe("ClarificationRecord", () => {
  const entries = [
    { id: "period", prompt: period.prompt, selected: ["今月"] },
    { id: "regions", prompt: regions.prompt, selected: ["東日本", "西日本"], otherText: "沖縄" },
    { id: "unit", prompt: "単位は？", selected: [] },
  ];

  it("会話の利用者の側に、確認事項と答えを残す", () => {
    act(() => root.render(<ClarificationRecord entries={entries} />));
    const record = byTestId("clarification-record");
    expect(record.dataset.outcome).toBe("answered");
    const group = record.querySelector('[role="group"]')!;
    expect(document.getElementById(group.getAttribute("aria-labelledby")!)?.textContent).toBe("回答前に確認した内容");
    const answers = Array.from(record.querySelectorAll("dd")).map((dd) => dd.textContent);
    expect(answers).toEqual(["今月", "東日本、西日本、その他: 沖縄", "（答えなし）"]);
  });

  it("スキップしたことを残す", () => {
    act(() => root.render(<ClarificationRecord entries={entries.slice(0, 1)} outcome="skipped" />));
    expect(byTestId("clarification-record-skipped").textContent).toBe("確認せずに回答しました（前提を示して回答）");
    expect(host.querySelectorAll("dd")).toHaveLength(0);
  });

  it("回答の詳細の折りたたみとしても出せる", () => {
    act(() => root.render(<ClarificationRecord entries={entries} variant="disclosure" />));
    const details = byTestId("clarification-record");
    expect(details.tagName).toBe("DETAILS");
    expect(details.querySelector("summary")?.textContent).toContain("回答前に確認した内容");
    expect((details as HTMLDetailsElement).open).toBe(false);
  });

  it("答えの文を組む", () => {
    expect(clarificationRecordAnswerText(entries[1])).toBe("東日本、西日本、その他: 沖縄");
    expect(clarificationRecordAnswerText({ id: "x", prompt: "?", selected: [], otherText: "  " })).toBe("（答えなし）");
    // 選択肢の無い確認事項の答えは「その他: 」を付けない（#3366）。
    expect(clarificationRecordAnswerText({ id: "u", prompt: "?", selected: [], otherText: "千円", freeTextOnly: true })).toBe(
      "千円"
    );
  });
});
