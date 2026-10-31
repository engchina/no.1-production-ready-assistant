import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { FlaskConical, Rocket, RotateCcw, Save } from "lucide-react";
import {
  Banner,
  Button,
  ButtonLink,
  Card,
  CardContent,
  FormActionBar,
  FormGrid,
  ListPicker,
  ListSkeleton,
  ObjectActionBar,
  TimedLoadingState,
  PageHeader,
  SaveErrorBanner,
  Section,
  StatusBadge,
  Switch,
  toast,
  useConfirm,
  type EntityAction,
  PageBody,
  SelectField,
  TabPanel,
  Tabs,
  TextareaField,
  TextField,
  type ListPickerItem,
  type SelectFieldOption,
  ApiErrorBanner,
  apiErrorMessage,
} from "@production-ready/ui";
import {
  agentApi,
  type ClarificationPolicy,
  type AgentDataScopes,
  type AgentOutputField,
  type AgentDraftSuggestion,
  type AgentProfile,
  type AgentTemplate,
  type AgentProfilePatchPayload,
  type AgentSkill,
  type BuiltinRuntimeModel,
  type SkillPolicy,
} from "@/lib/api";
import { matchesSearch } from "@/components/ListFilters";
import { AgentDataScopeSection, normalizeDataScopes } from "@/components/agents/AgentDataScopeSection";
import {
  AgentOutputFieldsSection,
  normalizeOutputFields,
  outputFieldErrors,
  type OutputFieldErrors,
} from "@/components/agents/AgentOutputFieldsSection";
import { AgentDraftAssistant } from "@/components/agents/AgentDraftAssistant";
import { AgentTemplatePicker } from "@/components/agents/AgentTemplatePicker";
import { SkillUpdateDiffDisclosure } from "@/components/agents/SkillUpdateDiff";
import { evaluationHref, useSkillUpdatePublish } from "@/components/agents/SkillUpdatesPanel";
import { useCanEditEvaluationSets } from "@/components/evaluation/AddToEvaluationCase";
import { formatNumber } from "@/lib/format";
import { t } from "@/lib/i18n";
import { useValuesChanged } from "@/lib/render-sync";
import { sameDraft, useDirtySources, useEditorLeaveGuard } from "@/lib/leave-guard";
import { AgentVersionBadges, AgentVersionsSection } from "@/pages/agents/AgentsPage";
import { AgentChat } from "@/pages/ChatPage";
import { focusField, formatDate } from "@/pages/shared/page-helpers";
import { skillSourceLabel, skillSourceVariant } from "@/pages/skills/skill-labels";

interface AgentDraft {
  name: string;
  description: string;
  instructions: string;
  skill_ids: string[];
  model_id: string;
  /** データの範囲（#1378。ID を並べ替え、空の接続は持たない形）。 */
  data_scopes: AgentDataScopes;
  /** 回答前の確認（#1839）。曖昧な質問の扱いと確認の上限（往復）。 */
  clarification_policy: ClarificationPolicy;
  clarification_max_rounds: number;
  /** スキルの更新の扱い（#1867。固定 / 常に最新）。 */
  skill_policy: SkillPolicy;
  /** 出力の項目（#2549）。 */
  output_fields: AgentOutputField[];
}

/**
 * 確認の上限（往復）の選択肢。0 は既定（3 回）。NL2SQL の業務プロファイルと同じく 0 か 2〜6（1 回は選べない）。
 * backend の `confirmation.DEFAULT_MAX_ROUNDS` / `MAX_ROUNDS_LIMIT`。
 */
const DEFAULT_CLARIFICATION_MAX_ROUNDS = 0;
const CLARIFICATION_MAX_ROUNDS_OPTIONS = [0, 2, 3, 4, 5, 6] as const;

function agentDraftOf(agent: AgentProfile | undefined): AgentDraft {
  return {
    name: agent?.name ?? "",
    description: agent?.description ?? "",
    instructions: agent?.instructions ?? "",
    // Skill の選択は集合なので並べ替えて比べる。
    skill_ids: [...(agent?.skill_ids ?? [])].sort(),
    model_id: agent?.model_id ?? "",
    data_scopes: normalizeDataScopes(agent?.data_scopes),
    clarification_policy: agent?.clarification_policy === "assume" ? "assume" : "confirm",
    clarification_max_rounds: agent?.clarification_max_rounds ?? DEFAULT_CLARIFICATION_MAX_ROUNDS,
    skill_policy: agent?.skill_policy === "latest" ? "latest" : "pinned",
    output_fields: normalizeOutputFields(agent?.output_fields ?? []),
  };
}

/** 編集画面のタブ（#2515）。「試す」は保存済みの下書きで会話する。 */
type EditorTab = "settings" | "try";

/** エディタの dirty を親へ知らせる。unmount 時は false を知らせる。 */
function useReportDirty(dirty: boolean, onDirtyChange: (dirty: boolean) => void) {
  const callbackRef = useRef(onDirtyChange);
  // 最新の callback を commit 時に入れる（render 中に ref を書かない）。下の effect より先に走る。
  useLayoutEffect(() => {
    callbackRef.current = onDirtyChange;
  });
  useEffect(() => {
    callbackRef.current(dirty);
  }, [dirty]);
  useEffect(() => () => callbackRef.current(false), []);
}

/**
 * 業務 Agent の全画面エディタ（A 型。`?id=new` / `?id=<agent id>`）。
 * 有効 / 無効は対象の操作（一覧の行メニュー・概要の ObjectActionBar）で切り替え、フォームの下書きには含めない
 * （切り替えで一覧を取り直しても、編集中の内容を上書きしない）。新規作成だけは初期状態をフォームで選ぶ。
 */
export function AgentEditorView({
  agent,
  availableSkills,
  skillsLoading,
  skillsError,
  models,
  defaultModelId,
  modelsLoading,
  actions,
  readOnly,
  sharing,
  onBack,
  onCreated,
}: {
  agent?: AgentProfile;
  availableSkills: AgentSkill[];
  /** Skill を取得中は「取得できません」と誤って出さず、読み込み中の表示にする。 */
  skillsLoading: boolean;
  skillsError: Error | null;
  /** 組み込み Runtime で選べるモデル（システム設定 > モデル の登録モデル。#754）。 */
  models: BuiltinRuntimeModel[];
  /** 空を選んだときに使う既定のテキストモデル（表示用）。 */
  defaultModelId: string;
  modelsLoading: boolean;
  actions: EntityAction[];
  /** 変更の権限がない利用者は閲覧だけ（保存を出さず、入力を無効にする）。 */
  readOnly: boolean;
  /** 個人の業務 Agent の共有（申請・承認。#2523）。概要の下に出す。 */
  sharing?: ReactNode;
  onBack: () => void;
  onCreated: (agent: AgentProfile) => void;
}) {
  const queryClient = useQueryClient();
  const saved = agentDraftOf(agent);
  const savedKey = JSON.stringify(saved);
  const [name, setName] = useState(saved.name);
  const [agentDescription, setAgentDescription] = useState(saved.description);
  const [instructions, setInstructions] = useState(saved.instructions);
  const [newEnabled, setNewEnabled] = useState(true);
  const [skillIds, setSkillIds] = useState<string[]>(saved.skill_ids);
  const [modelId, setModelId] = useState(saved.model_id);
  const [dataScopes, setDataScopes] = useState<AgentDataScopes>(saved.data_scopes);
  const [clarificationPolicy, setClarificationPolicy] = useState<ClarificationPolicy>(saved.clarification_policy);
  const [clarificationMaxRounds, setClarificationMaxRounds] = useState(saved.clarification_max_rounds);
  const [skillPolicy, setSkillPolicy] = useState<SkillPolicy>(saved.skill_policy);
  const [outputFields, setOutputFields] = useState<AgentOutputField[]>(saved.output_fields);
  const [outputErrors, setOutputErrors] = useState<OutputFieldErrors>({});
  // スキルの更新だけを公開する（#1867。下書きの変更は公開しない）。
  const publishSkillUpdates = useSkillUpdatePublish();
  const [baseline, setBaseline] = useState<AgentDraft>(saved);
  const [nameError, setNameError] = useState<string | null>(null);
  // 新規作成で選んだ業種テンプレート（#780）。作成した業務 Agent に残す（#810）。
  const [templateId, setTemplateId] = useState<string | null>(null);
  // テンプレートの評価ケースで評価セットを作るか（既定はオン。品質評価の権限を持つ利用者だけ。#810）。
  const canCreateEvaluationSet = useCanEditEvaluationSets();
  const [templateEvaluationSet, setTemplateEvaluationSet] = useState(true);
  // 説明から作った下書きの評価の問い（#2519）。作成と同時に評価セットとして保存できる（既定はオン）。
  const [draftCases, setDraftCases] = useState<AgentDraftSuggestion["evaluation_cases"]>([]);
  const [saveDraftCases, setSaveDraftCases] = useState(true);
  const confirmTemplate = useConfirm();
  // 「設定」と「試す」（#2515）。タブを移ってもフォームの入力はこの画面が持つので残る。
  const [tab, setTab] = useState<EditorTab>("settings");

  async function applyTemplate(template: AgentTemplate) {
    const touched = Boolean(name.trim() || agentDescription.trim() || instructions.trim() || skillIds.length);
    if (touched && templateId !== template.id) {
      const ok = await confirmTemplate({
        title: t("agent.template.replaceTitle"),
        description: t("agent.template.replaceDescription", { name: template.name }),
        confirmLabel: t("agent.template.replace"),
        tone: "warning",
      });
      if (!ok) return;
    }
    // 使えない（登録されていない）Skill は外して知らせる。
    const known = new Set(availableSkills.map((skill) => skill.id));
    const usable = template.skill_ids.filter((skillId) => known.has(skillId));
    const missing = template.skill_ids.filter((skillId) => !known.has(skillId));
    setName(template.name);
    setNameError(null);
    setAgentDescription(template.description);
    setInstructions(template.instructions);
    setSkillIds([...usable].sort());
    setOutputFields(normalizeOutputFields(template.output_fields ?? []));
    setOutputErrors({});
    setTemplateId(template.id);
    setDraftCases([]);
    toast.success(t("agent.template.applied", { name: template.name }), {
      description: missing.length ? t("agent.template.skillsMissing", { skills: missing.join("、") }) : undefined,
    });
  }

  /** 説明から作った下書きをフォームに入れる（#2519・#2561。入力済みなら確認してから置き換える。保存はしない）。 */
  async function applyDraftSuggestion(suggestion: AgentDraftSuggestion) {
    const touched = Boolean(
      name.trim() || agentDescription.trim() || instructions.trim() || skillIds.length || outputFields.length,
    );
    if (touched) {
      const ok = await confirmTemplate({
        title: t("agent.draftAssistant.replaceTitle"),
        description: t("agent.draftAssistant.replaceDescription"),
        confirmLabel: t("agent.template.replace"),
        tone: "warning",
      });
      if (!ok) return;
    }
    const known = new Set(availableSkills.map((skill) => skill.id));
    setName(suggestion.name);
    setNameError(null);
    setAgentDescription(suggestion.description);
    setInstructions(suggestion.instructions);
    setSkillIds(suggestion.skill_ids.filter((skillId) => known.has(skillId)).sort());
    setDataScopes(normalizeDataScopes(suggestion.data_scopes));
    setOutputFields(normalizeOutputFields(suggestion.output_fields ?? []));
    setTemplateId(null);
    setDraftCases(suggestion.evaluation_cases);
    toast.success(t("agent.draftAssistant.applied"));
  }

  const createAgent = useMutation({
    mutationFn: agentApi.createAgent,
    onSuccess: async (created) => {
      toast.success(t("agent.created"));
      setBaseline(draft);
      if (created.template_id && canCreateEvaluationSet && templateEvaluationSet) {
        // 評価セットの作成に失敗しても業務 Agent は作れている。品質評価の画面から作り直せる（#810）。
        try {
          const evaluationSet = await agentApi.createEvaluationSetFromTemplate(created.id);
          void queryClient.invalidateQueries({ queryKey: ["evaluation-sets"] });
          toast.success(t("evaluation.sets.fromTemplateCreated", { name: evaluationSet.name }));
        } catch (error) {
          toast.error(t("agent.template.evaluationSetFailed"), {
            description: apiErrorMessage(error, t("common.error.retryLater")),
          });
        }
      } else if (!created.template_id && draftCases.length && canCreateEvaluationSet && saveDraftCases) {
        // 説明から作った評価の問い（#2519）。失敗しても業務 Agent は作れている。品質評価の画面から作り直せる。
        try {
          const evaluationSet = await agentApi.createEvaluationSet({
            agent_id: created.id,
            name: t("agent.draftAssistant.setName", { name: created.name }),
            description: t("agent.draftAssistant.setDescription"),
            cases: draftCases,
          });
          void queryClient.invalidateQueries({ queryKey: ["evaluation-sets"] });
          toast.success(t("evaluation.sets.fromTemplateCreated", { name: evaluationSet.name }));
        } catch (error) {
          toast.error(t("agent.template.evaluationSetFailed"), {
            description: apiErrorMessage(error, t("common.error.retryLater")),
          });
        }
      }
      // 一覧を取り直してから作成した Agent のエディタへ移る（戻るで空の新規フォームへ戻さない）。
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      onCreated(created);
    },
  });
  const patchAgent = useMutation({
    mutationFn: (payload: AgentProfilePatchPayload) => agentApi.patchAgent(agent?.id ?? "", payload),
    onSuccess: (_data, payload) => {
      toast.success(t("agent.saved"));
      // 保存に成功した内容を基準にする（一覧の再取得を待たずに dirty を解く）。
      setBaseline({
        name: payload.name ?? "",
        description: payload.description ?? "",
        instructions: payload.instructions ?? "",
        skill_ids: [...(payload.skill_ids ?? [])].sort(),
        model_id: payload.model_id ?? "",
        data_scopes: normalizeDataScopes(payload.data_scopes),
        clarification_policy: payload.clarification_policy ?? "confirm",
        clarification_max_rounds: payload.clarification_max_rounds ?? DEFAULT_CLARIFICATION_MAX_ROUNDS,
        skill_policy: payload.skill_policy ?? "pinned",
        output_fields: normalizeOutputFields(payload.output_fields ?? []),
      });
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });
  const pending = createAgent.isPending || patchAgent.isPending;

  // 保存済みの内容が変わったときだけフォームを取り直す。有効状態の切替や他の Agent の保存による
  // 一覧の再取得で、編集中の内容を上書きしない（#87）。
  const isExisting = Boolean(agent);
  const savedChanged = useValuesChanged([isExisting, savedKey]);
  if (savedChanged && isExisting) {
    const next = JSON.parse(savedKey) as AgentDraft;
    setName(next.name);
    setAgentDescription(next.description);
    setInstructions(next.instructions);
    setSkillIds(next.skill_ids);
    setModelId(next.model_id);
    setDataScopes(next.data_scopes);
    setClarificationPolicy(next.clarification_policy);
    setClarificationMaxRounds(next.clarification_max_rounds);
    setSkillPolicy(next.skill_policy);
    setOutputFields(next.output_fields);
    setOutputErrors({});
    setBaseline(next);
    setNameError(null);
  }

  const draft: AgentDraft = {
    name,
    description: agentDescription,
    instructions,
    skill_ids: [...skillIds].sort(),
    model_id: modelId,
    data_scopes: normalizeDataScopes(dataScopes),
    clarification_policy: clarificationPolicy,
    clarification_max_rounds: clarificationMaxRounds,
    skill_policy: skillPolicy,
    output_fields: normalizeOutputFields(outputFields),
  };
  // Agent のフォームの dirty を 1 つの離脱ガードで守る（#87）。
  const dirtySources = useDirtySources();
  const formDirty = !sameDraft(draft, baseline) || (!agent && !newEnabled);
  useReportDirty(formDirty, (dirty) => dirtySources.report("agent", dirty));
  const { confirmClose } = useEditorLeaveGuard(dirtySources.anyDirty, pending);

  async function back() {
    if (await confirmClose()) onBack();
  }

  function setSkillSelected(skillId: string, selected: boolean) {
    setSkillIds((current) => {
      const next = current.filter((id) => id !== skillId);
      return selected ? [...next, skillId].sort() : next;
    });
  }

  /** 「変更を破棄」: 保存済みの内容（新規は空のフォーム）に戻す（#618。確認は出さない。RAG と同じ）。 */
  function discardChanges() {
    setName(baseline.name);
    setAgentDescription(baseline.description);
    setInstructions(baseline.instructions);
    setSkillIds(baseline.skill_ids);
    setModelId(baseline.model_id);
    setDataScopes(baseline.data_scopes);
    setClarificationPolicy(baseline.clarification_policy);
    setClarificationMaxRounds(baseline.clarification_max_rounds);
    setSkillPolicy(baseline.skill_policy);
    setOutputFields(baseline.output_fields);
    setOutputErrors({});
    setNewEnabled(true);
    setNameError(null);
    setTemplateId(null);
    setDraftCases([]);
  }

  // スキルの選択は ListPicker（検索・表示中をすべて選択・選択中だけ表示・キーボード。page-archetypes.md「大量の候補から選ぶ」）。
  const [skillSearch, setSkillSearch] = useState("");
  const skillItem = (skill: AgentSkill): ListPickerItem => ({
    key: skill.id,
    label: skill.name,
    textValue: skill.name,
    description: skill.description || skill.id,
    meta: (
      <span className="flex shrink-0 flex-wrap items-center gap-1.5">
        <StatusBadge variant={skillSourceVariant(skill.source)} label={skillSourceLabel(skill.source)} icon={false} />
        {skill.enabled ? null : <StatusBadge variant="neutral" label={t("agent.disabled")} />}
      </span>
    ),
  });
  // 割り当て済みで登録から消えたスキル（スキルの削除・宣言の変更による）。選択の一覧に出さないと
  // 外せず、保存・公開が「登録されていないスキル」で断られ続けるため、ID を名前にした行で出す（#925）。
  const knownSkillIds = new Set(availableSkills.map((skill) => skill.id));
  const missingSkillIds = skillsLoading || skillsError ? [] : skillIds.filter((skillId) => !knownSkillIds.has(skillId));
  const missingSkillItem = (skillId: string): ListPickerItem => ({
    key: skillId,
    label: skillId,
    textValue: skillId,
    description: t("agent.skillPicker.missingDescription", { id: skillId }),
    meta: <StatusBadge variant="danger" label={t("agent.skillPicker.missing")} />,
  });
  const missingSkillItems = missingSkillIds.map(missingSkillItem);
  const skillItems = [
    ...missingSkillItems.filter((item) => matchesSearch(skillSearch, [item.key])),
    ...availableSkills
      .filter((skill) => matchesSearch(skillSearch, [skill.name, skill.id, skill.description]))
      .map(skillItem),
  ];
  const selectedSkillItems = [
    ...missingSkillItems,
    ...availableSkills.filter((skill) => skillIds.includes(skill.id)).map(skillItem),
  ];
  // 公開は保存済みの下書きを版にする。画面の入力と違う内容を公開しないよう、未保存の変更があるあいだは
  // 押せなくする（#925）。
  const publishBlocked = formDirty && actions.some((action) => action.id === "publish" && action.visible !== false);
  const editorActions = actions.map((action) =>
    action.id === "publish" ? { ...action, disabled: Boolean(action.disabled) || formDirty } : action
  );

  function saveAgent() {
    setNameError(null);
    if (!name.trim()) {
      setNameError(t("agent.nameRequired"));
      focusField(`${fieldId}-agent-name`);
      return;
    }
    // 出力の項目の key の形・重なり・表示名の空は、保存の前に欄で知らせる（#2549）。
    const fieldErrors = outputFieldErrors(outputFields);
    setOutputErrors(fieldErrors);
    const firstInvalid = Object.keys(fieldErrors).map(Number).sort((a, b) => a - b)[0];
    if (firstInvalid !== undefined) {
      focusField(`${fieldId}-output-${firstInvalid}-${fieldErrors[firstInvalid].label ? "label" : "key"}`);
      return;
    }
    const payload = {
      name: name.trim(),
      description: agentDescription.trim(),
      instructions: instructions.trim(),
      skill_ids: skillIds,
      model_id: modelId,
      data_scopes: normalizeDataScopes(dataScopes),
      clarification_policy: clarificationPolicy,
      clarification_max_rounds: clarificationMaxRounds,
      skill_policy: skillPolicy,
      output_fields: normalizeOutputFields(outputFields),
    };
    if (agent) {
      setName(payload.name);
      setAgentDescription(payload.description);
      setInstructions(payload.instructions);
      patchAgent.mutate(payload);
      return;
    }
    createAgent.mutate({ ...payload, enabled: newEnabled, ...(templateId ? { template_id: templateId } : {}) });
  }

  const fieldId = agent?.id ?? "new";
  const title = agent ? agent.name : t("agent.create");
  // 試せるのは保存済みの業務 Agent を変更できる利用者（下書きの実行は Agent 管理だけ。#770）。
  const canTry = Boolean(agent) && !readOnly;
  const activeTab: EditorTab = canTry ? tab : "settings";
  // 試すのは保存した下書き。画面の入力と違う内容で試さないよう、未保存の変更があるあいだは送らない（公開と同じ）。
  const tryBlockedReason = !agent
    ? null
    : !agent.enabled
      ? t("agent.try.disabled")
      : formDirty
        ? t("agent.try.unsaved")
        : null;
  const error = createAgent.error ?? patchAgent.error;
  const saveAttemptKey = Math.max(createAgent.submittedAt, patchAgent.submittedAt);

  return (
    <>
      <PageHeader
        wide
        title={title}
        subtitle={agent ? agent.id : t("page.agents.subtitle")}
        // 一覧へ戻るは左上（#618）。確定（作成・保存と変更を破棄）はフォームの末尾の FormActionBar（#1786）。
        back={{ label: t("common.backToList"), ariaLabel: t("editor.backToListOf", { list: t("nav.agents") }), onClick: () => void back(), testId: "editor-back" }}
        tabs={
          canTry ? (
            <Tabs
              idPrefix="agent-editor"
              ariaLabel={t("agent.editor.tabs")}
              value={activeTab}
              onChange={(id) => setTab(id === "try" ? "try" : "settings")}
              items={[
                { id: "settings", label: t("agent.editor.tab.settings") },
                { id: "try", label: t("agent.editor.tab.try") },
              ]}
            />
          ) : undefined
        }
      />
      <PageBody wide className="space-y-6">
        {agent && canTry ? (
          <TabPanel idPrefix="agent-editor" id="try" value={activeTab} className="space-y-4">
            <Section title={t("agent.try.title")} description={t("agent.try.description")}>
              {tryBlockedReason ? (
                <div data-testid="agent-try-blocked">
                  <Banner
                    severity="warning"
                    action={
                      formDirty && agent.enabled ? (
                        <Button size="sm" icon={Save} loading={pending} onClick={saveAgent} data-testid="agent-try-save">
                          {t("common.save")}
                        </Button>
                      ) : undefined
                    }
                  >
                    {tryBlockedReason}
                  </Banner>
                </div>
              ) : null}
              <SaveErrorBanner
                message={error ? apiErrorMessage(error, t("common.error.save")) : null}
                attemptKey={saveAttemptKey}
                testId="agent-try-save-error"
              />
              <AgentChat tryTarget={{ agent, blocked: Boolean(tryBlockedReason) }} />
            </Section>
          </TabPanel>
        ) : null}
        <TabPanel idPrefix="agent-editor" id="settings" value={activeTab} className="space-y-6">
        {!agent && !readOnly ? (
          <>
            <AgentDraftAssistant disabled={pending} onApply={(suggestion) => void applyDraftSuggestion(suggestion)} />
            {draftCases.length ? (
              <Section
                title={t("agent.draftAssistant.casesTitle", { count: draftCases.length })}
                description={t("agent.draftAssistant.casesDescription")}
              >
                <div className="space-y-3" data-testid="agent-draft-cases">
                  <ol className="list-decimal space-y-2 pl-5 text-sm text-fg">
                    {draftCases.map((item, index) => (
                      <li key={`${index}-${item.question}`} className="break-words">
                        <span className="block">{item.question}</span>
                        <span className="block text-xs text-fg-muted">
                          {t("agent.draftAssistant.expected", { expected: item.expected })}
                        </span>
                      </li>
                    ))}
                  </ol>
                  {canCreateEvaluationSet ? (
                    <label
                      className="flex min-h-11 items-start gap-2 rounded-md border border-border px-3 py-2 text-sm text-fg"
                      data-testid="agent-draft-evaluation-set"
                    >
                      <input
                        type="checkbox"
                        checked={saveDraftCases}
                        onChange={(event) => setSaveDraftCases(event.target.checked)}
                        aria-describedby="agent-draft-evaluation-set-helper"
                        className="mt-0.5 h-4 w-4 shrink-0"
                      />
                      <span className="min-w-0">
                        <span className="block font-medium">
                          {t("agent.draftAssistant.saveCases", { count: draftCases.length })}
                        </span>
                        <span id="agent-draft-evaluation-set-helper" className="mt-0.5 block text-xs text-fg-muted">
                          {t("agent.draftAssistant.saveCasesHelper")}
                        </span>
                      </span>
                    </label>
                  ) : null}
                </div>
              </Section>
            ) : null}
            <AgentTemplatePicker
            selectedId={templateId}
            onApply={(template) => void applyTemplate(template)}
            evaluationSet={
              canCreateEvaluationSet
                ? { checked: templateEvaluationSet, onChange: setTemplateEvaluationSet }
                : undefined
            }
          />
          </>
        ) : null}
        {agent ? (
          <Section
            title={t("editor.overview")}
            actions={
              <ObjectActionBar
                actions={editorActions}
                ariaLabel={t("common.entityActions", { name: agent.name })}
                moreLabel={t("common.moreActions")}
                testId="agent-object-actions"
              />
            }
          >
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge
                variant={agent.enabled ? "success" : "neutral"}
                label={agent.enabled ? t("agent.enabled") : t("agent.disabled")}
              />
              <AgentVersionBadges agent={agent} />
              <span className="text-xs text-fg-muted">{`${t("common.updatedAt")}: ${formatDate(agent.updated_at)}`}</span>
            </div>
            {publishBlocked ? (
              <p className="text-xs text-fg-muted" data-testid="agent-save-before-publish">
                {t("agent.version.saveBeforePublish")}
              </p>
            ) : null}
            {agent.published_version !== null && agent.skill_updates?.length ? (
              // 公開中の版に固定したスキルが変わった（#1855）。利用者の Run は固定した中身で動く。
              <div data-testid="agent-skills-updated">
                <Banner severity="warning" title={t("agent.version.skillsUpdated")}>
                  <div className="min-w-0 space-y-2">
                    <p>
                      {t("agent.version.skillsUpdatedHint", {
                        version: agent.published_version,
                        skills: agent.skill_updates
                          .map((id) => availableSkills.find((skill) => skill.id === id)?.name ?? id)
                          .join("、"),
                      })}
                    </p>
                    {/* 差分 → 評価で比べる → スキルの更新だけを公開（#1867）。 */}
                    <div className="flex flex-wrap items-center gap-2">
                      {!readOnly ? (
                        <Button
                          size="sm"
                          icon={Rocket}
                          loading={publishSkillUpdates.isPending}
                          onClick={() => publishSkillUpdates.mutate([agent.id])}
                          data-testid="agent-publish-skill-updates"
                        >
                          {t("agent.skillUpdates.publishOne")}
                        </Button>
                      ) : null}
                      <ButtonLink
                        to={evaluationHref(agent.id)}
                        linkComponent={Link}
                        variant="secondary"
                        size="sm"
                        icon={FlaskConical}
                        testId="agent-skill-updates-evaluate"
                      >
                        {t("agent.skillUpdates.evaluateShort")}
                      </ButtonLink>
                    </div>
                    <SkillUpdateDiffDisclosure agentId={agent.id} testId="agent-skill-updates-diff" />
                  </div>
                </Banner>
              </div>
            ) : null}
            {agent.published_version !== null && agent.skills_pinned === false ? (
              <div data-testid="agent-skills-not-pinned">
                <Banner severity="info">{t("agent.version.skillsNotPinned", { version: agent.published_version })}</Banner>
              </div>
            ) : null}
            {agent.published_version === null ? (
              // 公開するまで利用者のチャット・Run には使えない（管理者は Run の画面の「下書きで実行」で試せる）。
              <Banner severity="info">{t("agent.version.unpublishedHint")}</Banner>
            ) : null}
          </Section>
        ) : null}
        {sharing}
        {/* 保存の対象（基本・スキル・データの範囲）を包み、その末尾に確定の行を置く（#1786）。 */}
        <div className="min-w-0 space-y-6">
          <fieldset disabled={readOnly} className="min-w-0 space-y-6">
            <Section title={t("agent.basic")}>
              <Card className="min-w-0">
                {/* 欄の並びは FormGrid（最大 60rem・2 列）。欄は値の種類の幅（名前 md・説明と指示は全幅・モデル lg。#1891）。 */}
                <CardContent className="pt-5">
                  <FormGrid>
                    <TextField
                      id={`${fieldId}-agent-name`}
                      label={t("agent.name")}
                      width="md"
                      required
                      error={nameError ?? undefined}
                      value={name}
                      onValueChange={(value) => {
                        setName(value);
                        setNameError(null);
                      }}
                    />
                    <TextField
                      id={`${fieldId}-agent-description`}
                      label={t("agent.description")}
                      width="full"
                      className="col-span-full"
                      value={agentDescription}
                      onValueChange={setAgentDescription}
                    />
                    <TextareaField
                      id={`${fieldId}-agent-instructions`}
                      label={t("agent.instructions")}
                      width="full"
                      className="col-span-full"
                      value={instructions}
                      onValueChange={setInstructions}
                      rows={8}
                    />
                    {/* 実行は組み込み Runtime（#754）。空は「既定のテキストモデル」（システム設定 > モデル）。 */}
                    <SelectField
                      id={`${fieldId}-agent-model`}
                      label={t("agent.model")}
                      helper={t("agent.modelHint")}
                      width="lg"
                      disabled={modelsLoading}
                      value={modelId}
                      emptyOptionLabel={
                        defaultModelId ? t("agent.modelDefaultWith", { model: defaultModelId }) : t("agent.modelDefault")
                      }
                      options={agentModelOptions(models, modelId)}
                      onValueChange={setModelId}
                    />
                    {!agent ? (
                      // 有効は Switch（スキルのエディタと同じ形）。作成した後は対象の操作（有効にする / 無効にする）で切り替える。
                      <label className="flex items-center gap-2 self-end text-sm text-fg">
                        <Switch
                          checked={newEnabled}
                          aria-label={t("agent.enabled")}
                          onCheckedChange={setNewEnabled}
                          data-testid="agent-new-enabled"
                        />
                        {t("agent.enabled")}
                      </label>
                    ) : null}
                  </FormGrid>
                </CardContent>
              </Card>
            </Section>
            <Section title={t("agent.skills")}>
              {skillsError ? <ApiErrorBanner error={skillsError} fallback={t("common.error.load")} /> : null}
              {missingSkillIds.length ? (
                <Banner severity="warning">
                  {t("agent.skillPicker.missingBanner", { skills: missingSkillIds.join("、") })}
                </Banner>
              ) : null}
              {skillsLoading ? (
                <TimedLoadingState label={t("loading.skills")} testId="agent-skills-loading">
                  <ListSkeleton rows={4} />
                </TimedLoadingState>
              ) : availableSkills.length || missingSkillIds.length ? (
                <ListPicker
                  id={`${fieldId}-agent-skills`}
                  label={t("agent.skillPicker.label")}
                  items={skillItems}
                  selectedKeys={new Set(skillIds)}
                  selectedItems={selectedSkillItems}
                  onToggle={(item, selected) => setSkillSelected(item.key, selected)}
                  onSelectMany={(items) =>
                    setSkillIds((current) => [...new Set([...current, ...items.map((item) => item.key)])].sort())
                  }
                  onClearSelection={() => setSkillIds([])}
                  search={{
                    id: `${fieldId}-agent-skill-search`,
                    label: t("agent.skillPicker.search"),
                    placeholder: t("agent.skillPicker.search"),
                    value: skillSearch,
                    onSearch: setSkillSearch,
                  }}
                  disabled={readOnly || pending}
                  labels={{
                    resultCount: ({ visible, total, selected }) =>
                      t("agent.skillPicker.count", {
                        visible: formatNumber(visible),
                        total: formatNumber(total),
                        selected: formatNumber(selected),
                      }),
                    emptyTitle: t("agent.skillPicker.empty"),
                    noResultsTitle: t("agent.skillPicker.noMatch"),
                    selectedEmpty: t("agent.skillPicker.selectedEmpty"),
                    clearSearch: t("common.clearSearch"),
                  }}
                  testId="agent-skill-picker"
                />
              ) : skillsError ? null : (
                <Banner severity="warning">{t("agent.skillsUnavailable")}</Banner>
              )}
              {/* スキルの更新の扱い（#1867。版の項目なので、変えたら公開で反映する）。 */}
              <SelectField<SkillPolicy>
                id={`${fieldId}-agent-skill-policy`}
                label={t("agent.skillPolicy.label")}
                helper={t("agent.skillPolicy.helper")}
                width="lg"
                value={skillPolicy}
                options={[
                  { value: "pinned", label: t("agent.skillPolicy.pinned") },
                  { value: "latest", label: t("agent.skillPolicy.latest") },
                ]}
                onValueChange={(value) => setSkillPolicy(value === "latest" ? "latest" : "pinned")}
                data-testid="agent-skill-policy"
              />
            </Section>
            <AgentDataScopeSection
              fieldId={fieldId}
              value={dataScopes}
              onChange={setDataScopes}
              readOnly={readOnly}
              disabled={pending}
            />
            <AgentOutputFieldsSection
              fieldId={fieldId}
              value={outputFields}
              onChange={(fields) => {
                setOutputFields(fields);
                setOutputErrors({});
              }}
              errors={outputErrors}
              readOnly={readOnly}
            />
            {/* 回答前の確認（#1839。3 製品で業務の単位に置く設定。用語は UX 契約 confirm-before-answer.md）。 */}
            <Section title={t("agent.clarification.title")} description={t("agent.clarification.description")}>
              <Card className="min-w-0">
                <CardContent className="pt-5">
                  <FormGrid data-testid="agent-clarification">
                    <SelectField
                      id={`${fieldId}-agent-clarification-policy`}
                      label={t("agent.clarification.policy")}
                      helper={t("agent.clarification.policyHint")}
                      width="md"
                      value={clarificationPolicy}
                      options={[
                        { value: "confirm", label: t("agent.clarification.policy.confirm") },
                        { value: "assume", label: t("agent.clarification.policy.assume") },
                      ]}
                      onValueChange={(value) => setClarificationPolicy(value === "assume" ? "assume" : "confirm")}
                    />
                    <SelectField
                      id={`${fieldId}-agent-clarification-max-rounds`}
                      label={t("agent.clarification.maxRounds")}
                      helper={t("agent.clarification.maxRoundsHint")}
                      width="sm"
                      // 前提を示して回答するときは確認しないので、上限は使わない。
                      disabled={clarificationPolicy === "assume"}
                      value={String(clarificationMaxRounds)}
                      options={CLARIFICATION_MAX_ROUNDS_OPTIONS.map((rounds) => ({
                        value: String(rounds),
                        label:
                          rounds === 0
                            ? t("agent.clarification.maxRounds.default")
                            : t("agent.clarification.maxRounds.value", { count: rounds }),
                      }))}
                      onValueChange={(value) => setClarificationMaxRounds(Number(value))}
                    />
                  </FormGrid>
                </CardContent>
              </Card>
            </Section>
          </fieldset>
          {/* 保存の失敗は確定の行の直上の 1 か所だけ（messaging.md §3.3.1。#585 / #1786）。 */}
          <SaveErrorBanner
            message={error ? apiErrorMessage(error, t("common.error.save")) : null}
            attemptKey={saveAttemptKey}
            testId="agent-save-error"
          />
          {!readOnly ? (
            // 確定はフォームの末尾の左（作成・保存 → 変更を破棄）。複数のカードを 1 つの保存で確定するので、
            // 最後のカードの下に置く。画面の下端に貼り付けない（#1786 / #2715）。
            <FormActionBar
              ariaLabel={t("editor.actions.formLabel")}
              testId="agent-form-actions"
              primaryActions={[
                {
                  id: "save",
                  label: agent ? t("common.save") : t("common.create"),
                  icon: Save,
                  loading: pending,
                  onClick: saveAgent,
                },
              ]}
              secondaryActions={[
                {
                  id: "discard",
                  label: t("editor.actions.discard"),
                  icon: RotateCcw,
                  disabled: !formDirty || pending,
                  onClick: discardChanges,
                },
              ]}
            />
          ) : null}
        </div>
        {agent ? <AgentVersionsSection agent={agent} readOnly={readOnly} /> : null}
        </TabPanel>
      </PageBody>
    </>
  );
}

/** モデルの選択肢（空 =「既定のテキストモデル」は emptyOptionLabel）。登録から消えた保存済みの値も残す。 */
function agentModelOptions(models: BuiltinRuntimeModel[], current: string): SelectFieldOption[] {
  const options: SelectFieldOption[] = models.map((model) => ({
    value: model.model_id,
    label: model.display_name,
  }));
  if (current && !models.some((model) => model.model_id === current)) {
    options.push({ value: current, label: t("agent.modelMissing", { model: current }) });
  }
  return options;
}
