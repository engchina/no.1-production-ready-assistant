import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { NAV_SECTIONS, settingsSubtitleKey } from "@/components/layout/nav-config";
import { ja, t, type I18nKey } from "@/lib/i18n";

import { SERVICE_CATEGORY_ROUTES, serviceStages } from "./service-stages";

// サービス管理の工程の並び・名前・説明は、サイドナビと設定画面を正本にする（#638）。
// ナビを並べ替え・改名したとき、backend のカタログに工程を足したとき、設定画面の説明を変えたときのずれを検出する。

const readSource = (path: string) => readFileSync(resolve(__dirname, path), "utf8");
/** 正規表現の特殊文字をすべて escape する（`.` だけでは `\` などが残る）。 */
const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const catalogSource = readSource("../../../../backend/app/services/catalog.py");
const appSource = readSource("../../App.tsx");

/** backend の `ServiceCategory`（Literal）の値。 */
const backendCategories = [
  ...(catalogSource.match(/^ServiceCategory = Literal\[([\s\S]*?)^\]/m)?.[1] ?? "").matchAll(/"([a-z_]+)"/g),
].map((match) => match[1]);

/** backend の SERVICE_CATALOG の (category, label_key)。 */
const catalogEntries = [
  ...catalogSource.matchAll(/category="([a-z_]+)",\s*profile="[a-z]+",\s*url_field="[a-z_]+",\s*label_key="([^"]+)"/g),
].map((match) => ({ category: match[1], labelKey: match[2] }));

const pipelineItems = NAV_SECTIONS.find((section) => section.titleKey === "nav.section.pipeline")?.items ?? [];

describe("サービス管理の工程とサイドナビ", () => {
  const stages = serviceStages();

  it("backend のカテゴリはすべて、サイドナビの「取込・検索・回答設定」の項目に対応する", () => {
    expect(backendCategories.length).toBeGreaterThan(0);
    expect(Object.keys(SERVICE_CATEGORY_ROUTES).sort()).toEqual([...backendCategories].sort());
    const pipelineRoutes = pipelineItems.map((item) => item.href);
    for (const route of Object.values(SERVICE_CATEGORY_ROUTES)) {
      expect(pipelineRoutes).toContain(route);
    }
    expect(stages.map((stage) => stage.category).sort()).toEqual([...backendCategories].sort());
  });

  it("工程の並びと名前は、サイドナビの並びと表示名と同じ", () => {
    const expected = pipelineItems
      .filter((item) => Object.values(SERVICE_CATEGORY_ROUTES).some((route) => route === item.href))
      .map((item) => t(item.sidebarLabelKey ?? item.labelKey));
    expect(stages.map((stage) => t(stage.labelKey))).toEqual(expected);
    // 現在のナビでの並び（ナビを変えたら、この期待値も合わせて見直す）。
    expect(expected).toEqual(["ファイル準備", "文書解析"]);
  });

  it("backend のカタログも、工程の並び（サイドナビの並び）の順に並ぶ", () => {
    const order = stages.map((stage) => stage.category);
    expect(backendCategories).toEqual(order);
    const positions = catalogEntries.map((entry) => order.indexOf(entry.category as (typeof order)[number]));
    expect(positions.every((position) => position >= 0)).toBe(true);
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
  });

  it("工程の説明は、対応する設定画面の PageHeader の説明と同じキー", () => {
    for (const item of pipelineItems) {
      const stage = stages.find((candidate) => SERVICE_CATEGORY_ROUTES[candidate.category] === item.href);
      if (!stage) continue;
      const header = appSource.match(
        new RegExp(`title=\\{t\\("${escapeRegExp(item.labelKey)}"\\)\\}\\s*subtitle=\\{t\\("([^"]+)"\\)\\}`)
      );
      expect(header?.[1], item.href).toBeDefined();
      expect(stage.descriptionKey).toBe(header?.[1]);
      expect(settingsSubtitleKey(item)).toBe(header?.[1]);
    }
  });
});

describe("サービス管理のサービス名", () => {

  it("カタログの label_key はすべて i18n にある", () => {
    expect(catalogEntries.length).toBeGreaterThan(0);
    for (const entry of catalogEntries) {
      expect(entry.labelKey in ja, entry.labelKey).toBe(true);
    }
  });

  it("ファイル準備・文書解析のサービスは、設定画面の方式・エンジンと同じ名前", () => {
    const sameAs: Record<string, I18nKey> = {
      "settings.services.item.preprocessOfficeToPdf": "settings.preprocess.profile.office_to_pdf",
      "settings.services.item.preprocessCsvToJsonl": "settings.preprocess.profile.csv_to_jsonl",
      "settings.services.item.preprocessExcelToJsonl": "settings.preprocess.profile.excel_to_jsonl",
      "settings.services.item.preprocessWordToJsonl": "settings.preprocess.profile.word_to_jsonl",
      "settings.services.item.parserStructuredRecordsJsonl":
        "settings.parserAdapters.backend.structured_records_jsonl",
      "settings.services.item.parserXmlTreeJson": "settings.parserAdapters.backend.xml_tree_json",
      "settings.services.item.parserOciDocumentUnderstanding":
        "settings.parserAdapters.backend.oci_document_understanding",
    };
    for (const [serviceKey, settingsKey] of Object.entries(sameAs)) {
      expect(t(serviceKey as I18nKey), serviceKey).toBe(t(settingsKey));
    }
  });
});
