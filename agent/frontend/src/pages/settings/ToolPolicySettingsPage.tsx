import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { PlugZap, Save } from "lucide-react";
import {
  Banner,
  ButtonLink,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  EmptyState,
  FormActionBar,
  FormStatus,
  FormSkeleton,
  ListToolbar,
  TableSkeleton,
  PageHeader,
  StatusBadge,
  toast,
  PageBody,
  SelectField,
  type DataTableColumn,
  type SelectFieldOption,
} from "@production-ready/ui";
import { useAuth } from "@/components/security/AuthProvider";
import { agentApi, type ToolPolicyConnection, type ToolPolicyTool } from "@/lib/api";
import { PagedDataTable, QueryState } from "@/components/ListViews";
import { ListSearchField, listCount, matchesSearch, NoMatchState, useListSearch } from "@/components/ListFilters";
import { t } from "@/lib/i18n";
import { useValuesChanged } from "@/lib/render-sync";
import { permissionView, policyDecisionView } from "@/lib/status-labels";
import { canOpenRoute } from "@/lib/route-permissions";
import { APP_ROUTES } from "@/lib/routes";
import { sameDraft, useSettingsLeaveGuard } from "@/lib/leave-guard";
import { NonPersistentStorageNotice } from "@/components/system/StorageNotice";

type ToolPolicyChoice = "default" | "allow" | "ask" | "deny";

function SettingsSaveBar({
  section,
  onSave,
  saving,
  error,
}: {
  section: string;
  onSave: () => void;
  saving: boolean;
  error: Error | null;
}) {
  return (
    <FormActionBar
      ariaLabel={t("settings.saveActions", { section })}
      primaryActions={[{ id: "save", label: t("common.save"), icon: Save, loading: saving, onClick: onSave }]}
      status={error ? <FormStatus tone="danger" message={t("settings.saveFailed", { reason: error.message })} /> : null}
    />
  );
}

interface ToolPolicyDraft {
  defaultMode: "approval" | "deny";
  policies: Array<[string, ToolPolicyChoice]>;
}

/** 判定の理由（#1832）。個別に指定していれば「ツール権限で指定」、既定に従うなら backend の理由。 */
function policyReasonLabel(tool: ToolPolicyTool, choice: ToolPolicyChoice | undefined): string {
  if (choice && choice !== "default") return t("settings.toolPolicy.reason.policy");
  switch (tool.default_reason) {
    case "standard":
      return t("settings.toolPolicy.reason.standard");
    case "read_only":
      return tool.server_id === "control-plane"
        ? t("settings.toolPolicy.reason.read_only")
        : t("settings.toolPolicy.reason.annotation");
    case "untrusted_annotation":
      return t("settings.toolPolicy.reason.untrusted_annotation");
    case "default_mode":
      return t("settings.toolPolicy.reason.default_mode");
    default:
      return "-";
  }
}

function connectionLabel(tool: ToolPolicyTool): string {
  return tool.server_id === "control-plane"
    ? t("settings.toolPolicy.builtin")
    : tool.server_label || tool.server_id;
}

/** 一覧を取れなかった MCP 接続（#1814）。保存したポリシーは一覧に無い行として残る。 */
function ConnectionErrors({
  connections,
  canOpenMcpConnections,
}: {
  connections: ToolPolicyConnection[];
  canOpenMcpConnections: boolean;
}) {
  return (
    <div data-testid="tool-policy-connection-errors">
      <Banner
        severity="warning"
        title={t("settings.toolPolicy.connectionErrorsTitle")}
        action={
          canOpenMcpConnections ? (
            <ButtonLink
              to={APP_ROUTES.settingsMcpConnections}
              linkComponent={Link}
              size="sm"
              icon={PlugZap}
              testId="tool-policy-open-mcp-connections"
            >
              {t("settings.toolPolicy.openMcpConnections")}
            </ButtonLink>
          ) : undefined
        }
      >
        <div className="space-y-2">
          <ul className="space-y-1">
            {connections.map((connection) => (
              <li key={connection.server_id} className="break-words [overflow-wrap:anywhere]">
                {t("settings.toolPolicy.connectionError", { label: connection.label, error: connection.error ?? "" })}
              </li>
            ))}
          </ul>
          <p>{t("settings.toolPolicy.connectionErrorsBody")}</p>
        </div>
      </Banner>
    </div>
  );
}

/** 「既定」は未指定と同じ意味なので外し、ツール名で並べて比べる。 */
function toolPolicyDraftOf(
  defaultMode: "approval" | "deny",
  policies: Record<string, ToolPolicyChoice>
): ToolPolicyDraft {
  return {
    defaultMode,
    policies: Object.entries(policies)
      .filter(([, policy]) => policy !== "default")
      .sort(([left], [right]) => left.localeCompare(right)),
  };
}

export function ToolPolicySettingsPage() {
  const queryClient = useQueryClient();
  // 組み込みのツールと MCP 接続のツール（`tools/list`。取れない接続は理由つき。#1814）。
  const tools = useQuery({ queryKey: ["settings", "tool-policy", "tools"], queryFn: agentApi.listToolPolicyTools });
  const { hasPermission } = useAuth();
  const canOpenMcpConnections = canOpenRoute(APP_ROUTES.settingsMcpConnections, hasPermission);
  const settings = useQuery({
    queryKey: ["settings", "tool-policy"],
    queryFn: agentApi.getToolPolicySettings,
  });
  const mutation = useMutation({
    mutationFn: agentApi.patchToolPolicySettings,
    onSuccess: () => {
      toast.success(t("common.saved"));
      void queryClient.invalidateQueries({ queryKey: ["settings", "tool-policy"] });
    },
  });
  const [defaultMode, setDefaultMode] = useState<"approval" | "deny">("approval");
  const [toolPolicies, setToolPolicies] = useState<Record<string, ToolPolicyChoice>>({});
  const [baseline, setBaseline] = useState<ToolPolicyDraft | null>(null);

  // server 値が変わったレンダーで、フォームと比較の基準を server 値に戻す（effect で setState しない）。
  const serverChanged = useValuesChanged([settings.data]);
  if (serverChanged && settings.data) {
    const current = settings.data;
    const nextPolicies: Record<string, ToolPolicyChoice> = {};
    current.allow.forEach((name) => {
      nextPolicies[name] = "allow";
    });
    current.ask.forEach((name) => {
      nextPolicies[name] = "ask";
    });
    current.deny.forEach((name) => {
      nextPolicies[name] = "deny";
    });
    setDefaultMode(current.default_mode);
    setToolPolicies(nextPolicies);
    setBaseline(toolPolicyDraftOf(current.default_mode, nextPolicies));
  }

  const draft = toolPolicyDraftOf(defaultMode, toolPolicies);
  useSettingsLeaveGuard(baseline !== null && !sameDraft(draft, baseline), mutation.isPending);

  // ツールは件数が増えるので、他の一覧と同じく検索・ページングの表にする（page-archetypes.md §0-4 / #535。#818）。
  const [toolQuery, setToolQuery] = useListSearch("toolPolicy");
  const allTools = useMemo(() => tools.data?.tools ?? [], [tools.data?.tools]);
  const visibleTools = allTools.filter((tool) =>
    matchesSearch(toolQuery, [tool.name, tool.description, connectionLabel(tool)])
  );
  const failedConnections = (tools.data?.connections ?? []).filter((connection) => connection.error);

  function setPolicy(toolName: string, policy: ToolPolicyChoice) {
    setToolPolicies((current) => ({ ...current, [toolName]: policy }));
  }

  // 「既定に従う」には、選んだときの判定（MCP のツールは readOnlyHint と既定の動作で決まる）を添える。
  const toolPolicyOptions = (tool: ToolPolicyTool): SelectFieldOption<ToolPolicyChoice>[] => [
    {
      value: "default",
      label: tool.default_decision
        ? t("settings.toolPolicy.defaultWith", { decision: policyDecisionView(tool.default_decision).label })
        : t("settings.toolPolicy.default"),
    },
    { value: "allow", label: t("settings.toolPolicy.allow") },
    { value: "ask", label: t("settings.toolPolicy.ask") },
    { value: "deny", label: t("settings.toolPolicy.deny") },
  ];

  const toolPolicyColumns: DataTableColumn<ToolPolicyTool>[] = [
    {
      key: "name",
      header: t("common.tool"),
      rowHeader: true,
      render: (tool) => (
        <div className="min-w-0 space-y-1">
          <p className="break-all text-sm font-medium text-fg">{tool.name}</p>
          {tool.description ? (
            <p className="break-words text-xs leading-5 text-fg-muted [overflow-wrap:anywhere]">{tool.description}</p>
          ) : null}
          {tool.available ? null : (
            <StatusBadge variant="neutral" label={t("settings.toolPolicy.unavailable")} icon={false} />
          )}
        </div>
      ),
    },
    {
      key: "connection",
      header: t("settings.toolPolicy.connection"),
      className: "text-xs text-fg",
      render: (tool) => connectionLabel(tool),
    },
    {
      key: "permission",
      header: t("common.permission"),
      render: (tool) =>
        tool.permission_level ? <StatusBadge {...permissionView(tool.permission_level)} icon={false} /> : "-",
    },
    {
      key: "side_effects",
      header: t("settings.toolPolicy.sideEffects"),
      render: (tool) =>
        tool.side_effects ? <StatusBadge variant="warning" label={t("status.sideEffects")} icon={false} /> : "-",
    },
    {
      key: "policy",
      header: t("settings.toolPolicy.policy"),
      className: "w-56",
      render: (tool) => (
        <SelectField<ToolPolicyChoice>
          id={`tool-policy-${tool.name}`}
          label={t("settings.toolPolicy.policyFor", { name: tool.name })}
          labelHidden
          size="sm"
          value={toolPolicies[tool.name] ?? "default"}
          options={toolPolicyOptions(tool)}
          onValueChange={(value) => setPolicy(tool.name, value)}
        />
      ),
    },
    {
      // 判定の理由（ツール権限の指定 / 注釈 / 既定。#1832）。選んだ値に合わせて変わる。
      key: "reason",
      header: t("settings.toolPolicy.reasonHeader"),
      className: "text-xs leading-5 text-fg-muted",
      render: (tool) => (
        <span data-testid={`tool-policy-reason-${tool.name}`}>{policyReasonLabel(tool, toolPolicies[tool.name])}</span>
      ),
    },
  ];

  function save() {
    const allow: string[] = [];
    const ask: string[] = [];
    const deny: string[] = [];
    for (const [toolName, policy] of Object.entries(toolPolicies)) {
      if (policy === "allow") {
        allow.push(toolName);
      } else if (policy === "ask") {
        ask.push(toolName);
      } else if (policy === "deny") {
        deny.push(toolName);
      }
    }
    const submitted = draft;
    mutation.mutate(
      {
        default_mode: defaultMode,
        allow: allow.sort(),
        ask: ask.sort(),
        deny: deny.sort(),
      },
      { onSuccess: () => setBaseline(submitted) }
    );
  }

  return (
    <>
      <PageHeader wide title={t("nav.settingsToolPolicy")} subtitle={t("page.settings.toolPolicy.subtitle")} />
      <PageBody wide>
        <NonPersistentStorageNotice />
        <div className="space-y-5">
        <QueryState query={settings} loadingLabel={t("loading.settings")} skeleton={<FormSkeleton fields={4} />}>
          <Card className="min-w-0">
            <CardHeader>
              <CardTitle>{t("nav.settingsToolPolicy")}</CardTitle>
              <CardDescription>{t("page.settings.toolPolicy.subtitle")}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <SelectField<"approval" | "deny">
                id="tool-policy-default-mode"
                label={t("settings.toolPolicy.defaultMode")}
                width="md"
                value={defaultMode}
                options={[
                  { value: "approval", label: t("settings.toolPolicy.defaultModeApproval") },
                  { value: "deny", label: t("settings.toolPolicy.defaultModeDeny") },
                ]}
                onValueChange={setDefaultMode}
              />

              {failedConnections.length ? (
                <ConnectionErrors connections={failedConnections} canOpenMcpConnections={canOpenMcpConnections} />
              ) : null}
              <ListToolbar
                search={
                  <ListSearchField
                    id="tool-policy-search"
                    label={t("settings.toolPolicy.search")}
                    value={toolQuery}
                    onSearch={setToolQuery}
                  />
                }
                count={tools.data ? listCount(allTools.length, visibleTools.length, Boolean(toolQuery.trim())) : undefined}
                testId="tool-policy-toolbar"
              />
              <QueryState
                query={tools}
                loadingLabel={t("loading.tools")}
                skeleton={<TableSkeleton columns={4} />}
                testId="tool-policy-tools-loading"
              >
                <PagedDataTable<ToolPolicyTool>
                  rows={visibleTools}
                  columns={toolPolicyColumns}
                  getRowKey={(tool) => tool.name}
                  rowProps={() => ({ className: "align-top" })}
                  tableClassName="w-full min-w-[68rem]"
                  ariaLabel={t("nav.settingsToolPolicy")}
                  resetKey={toolQuery}
                  empty={
                    allTools.length ? (
                      <NoMatchState title={t("tool.noMatch")} onClear={() => setToolQuery("")} />
                    ) : (
                      <EmptyState title={t("common.empty.title")} />
                    )
                  }
                />
              </QueryState>

              <SettingsSaveBar
                section={t("nav.settingsToolPolicy")}
                onSave={save}
                saving={mutation.isPending}
                error={mutation.error}
              />
            </CardContent>
          </Card>
        </QueryState>
        </div>
      </PageBody>
    </>
  );
}
