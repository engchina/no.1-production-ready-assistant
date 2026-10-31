import { Bot, Check, DatabaseZap, Sparkles } from "lucide-react";
import { useId } from "react";

import { Button } from "@production-ready/ui";

import { t } from "@/lib/i18n";
import type { Nl2SqlEngine } from "../types";

const ENGINE_OPTIONS: Array<{ value: Nl2SqlEngine; label: string; description: string }> = [
  {
    value: "select_ai",
    label: t("nl2sql.engine.selectAi"),
    description: t("nl2sql.engine.selectAi.desc"),
  },
  {
    value: "select_ai_agent",
    label: t("nl2sql.engine.agent"),
    description: t("nl2sql.engine.agent.desc"),
  },
  {
    value: "enterprise_ai_direct",
    label: t("nl2sql.engine.direct"),
    description: t("nl2sql.engine.direct.desc"),
  },
];

function EngineIcon({ engine }: { engine: Nl2SqlEngine }) {
  if (engine === "select_ai_agent") return <Bot size={16} aria-hidden="true" />;
  if (engine === "select_ai") return <DatabaseZap size={16} aria-hidden="true" />;
  return <Sparkles size={16} aria-hidden="true" />;
}

export function EngineSelector({
  value,
  onChange,
  disabled,
}: {
  value: Nl2SqlEngine;
  onChange: (value: Nl2SqlEngine) => void;
  disabled?: boolean;
}) {
  const helpId = useId();
  return (
    <fieldset className="min-w-0 space-y-3" aria-describedby={helpId}>
      <legend className="text-base font-semibold text-fg">{t("nl2sql.engine.label")}</legend>
      <p id={helpId} className="text-sm leading-relaxed text-fg-muted">{t("nl2sql.engine.help")}</p>
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {ENGINE_OPTIONS.map((option) => {
          const selected = option.value === value;
          return (
            /* 排他選択の segmented control。primary は画面の主 CTA(SQL を生成して実行)専用なので
               (button spec §0.2/§6)、選択状態は枠線 + チェックアイコンで表現する。
               説明文を含むカード型の選択肢なので、操作部品の高さの段（#613）ではなく内容の高さにする。 */
            // アイコンとチェックは名前の 1 行目の中心にそろえる（items-start + 名前と同じ文字の大きさの行の高さの箱。#2371）。
            // oxlint-disable-next-line design-system/restricted-syntax
            <Button className="h-auto min-h-[64px] items-start justify-start whitespace-normal py-3 text-left [&>span]:whitespace-normal"
              key={option.value}
              type="button"
              variant="secondary"
              size="md"
              disabled={disabled}
              aria-pressed={selected}
              onClick={() => onChange(option.value)}
            >
              <span className="pr-icon-line text-base">
                <EngineIcon engine={option.value} />
              </span>
              <span className="grid min-w-0 flex-1 gap-1 text-left">
                <span className="text-base font-semibold">{option.label}</span>
                <span className="text-sm font-normal leading-relaxed opacity-80">{option.description}</span>
              </span>
              {selected ? (
                <span className="pr-icon-line ml-auto text-base">
                  <Check size={16} aria-hidden="true" />
                </span>
              ) : null}
            </Button>
          );
        })}
      </div>
    </fieldset>
  );
}
