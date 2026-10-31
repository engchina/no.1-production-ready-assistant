import { describe, expect, it } from "vitest";

import { t } from "@/lib/i18n";

import {
  SERVICE_STATUS_BADGE,
  serviceCanRestart,
  serviceExecutionPolicyLabelKey,
  servicePrimaryAction,
  serviceStoppedHintKey,
} from "./ServicesManagementClient";

describe("ServicesManagementClient service policy helpers", () => {
  it("maps execution policies to compact badge labels", () => {
    expect(t(serviceExecutionPolicyLabelKey("default_adapter"))).toBe("既定で使用");
    expect(t(serviceStoppedHintKey("default_adapter"))).toContain("解析が失敗します");
    expect(t("settings.services.confirm.stop.defaultDescription", { service: "Docling" })).toContain("既定の解析エンジン");
    expect(serviceExecutionPolicyLabelKey("selected_adapter")).toBe(
      "settings.services.executionPolicy.selectedAdapter"
    );
  });

  it("maps stopped services to the right clarification text", () => {
    expect(serviceStoppedHintKey("selected_adapter")).toBe(
      "settings.services.optionalStoppedHint.selectedAdapter"
    );
  });

  // 行に常に出す主操作は状態に応じて 1 つだけ（#158）。
  it("picks one state-based primary lifecycle action per row", () => {
    expect(servicePrimaryAction("running")).toBe("stop");
    expect(servicePrimaryAction("degraded")).toBe("stop");
    expect(servicePrimaryAction("stopped")).toBe("start");
    expect(servicePrimaryAction("unconfigured")).toBe("start");
    expect(servicePrimaryAction("loading")).toBe("start");
    expect(servicePrimaryAction("error")).toBe("start");
  });

  // systemd の unit の状態（#286）。起動中は unit が動いているので「停止」、失敗・未登録は「起動」。
  it("maps systemd unit states to the primary action and restart availability", () => {
    expect(servicePrimaryAction("starting")).toBe("stop");
    expect(servicePrimaryAction("failed")).toBe("start");
    expect(servicePrimaryAction("not_installed")).toBe("start");
    expect(serviceCanRestart("running")).toBe(true);
    expect(serviceCanRestart("degraded")).toBe(true);
    expect(serviceCanRestart("starting")).toBe(true);
    expect(serviceCanRestart("stopped")).toBe(false);
    expect(serviceCanRestart("failed")).toBe(false);
    expect(serviceCanRestart("not_installed")).toBe(false);
  });

  it("labels systemd states and journald logs without docker wording", () => {
    expect(t("settings.services.status.starting")).toBe("起動中");
    expect(t("settings.services.status.failed")).toBe("起動失敗");
    expect(t("settings.services.status.not_installed")).toBe("未登録");
    expect(
      t("settings.services.logs.source.journald", {
        unit: "production-ready-rag-parser-docling.service",
        lines: "200",
      })
    ).toBe("journalctl -u production-ready-rag-parser-docling.service / 最新 200 行");
    for (const key of [
      "settings.services.overview.description",
      "settings.services.mode.local",
      "settings.services.mode.production",
      "settings.services.mode.local.hint",
      "settings.services.mode.deployed.hint",
      "settings.services.modelCache.hint",
    ] as const) {
      expect(t(key)).not.toMatch(/docker|compose|コンテナ/i);
    }
    // 既定の解析エンジンは Docling（#286）。
    expect(t("settings.services.cpuNote")).toContain("文書解析の全体設定");
  });

  // 稼働状態は共有の StatusBadge の variant で出す（messaging.md §10.2。#723）。
  it("maps runtime statuses to shared StatusBadge variants", () => {
    expect(SERVICE_STATUS_BADGE.running.variant).toBe("success");
    expect(SERVICE_STATUS_BADGE.degraded.variant).toBe("warning");
    expect(SERVICE_STATUS_BADGE.failed.variant).toBe("danger");
    expect(SERVICE_STATUS_BADGE.error.variant).toBe("danger");
    expect(SERVICE_STATUS_BADGE.stopped.variant).toBe("neutral");
    // 非推奨の pending（warning の別名）は使わない。
    for (const meta of Object.values(SERVICE_STATUS_BADGE)) {
      expect(meta.variant).not.toBe("pending");
    }
  });
});
