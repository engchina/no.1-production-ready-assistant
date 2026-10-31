import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  EmptyState,
  ListToolbar,
  TableSkeleton,
  PageHeader,
  Section,
  StatusBadge,
  type DataTableColumn,
  PageBody,
  RowTitleButton,
} from "@production-ready/ui";
import { agentApi, type ToolDefinition } from "@/lib/api";
import { AgentSplitPane } from "@/components/EntityLayout";
import { PagedDataTable, QueryState } from "@/components/ListViews";
import {
  ListSearchField,
  listCount,
  matchesSearch,
  NoMatchState,
  useListSearch,
} from "@/components/ListFilters";
import { t } from "@/lib/i18n";
import { permissionView } from "@/lib/status-labels";
import { JsonPanel } from "@/pages/shared/page-helpers";

export function ToolsPage() {
  const tools = useQuery({ queryKey: ["tools"], queryFn: agentApi.listTools });
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const list = tools.data?.tools ?? [];
  const [toolQuery, setToolQuery] = useListSearch("tools");
  const visibleTools = list.filter((tool) => matchesSearch(toolQuery, [tool.name, tool.description]));
  const selected = visibleTools.find((tool) => tool.name === selectedName) ?? visibleTools[0];

  const columns: DataTableColumn<ToolDefinition>[] = [
    {
      key: "name",
      header: t("common.tool"),
      rowHeader: true,
      render: (tool) => (
        <RowTitleButton
          title={tool.name}
          current={tool.name === selected?.name}
          onClick={() => setSelectedName(tool.name)}
        />
      ),
    },
    {
      key: "permission",
      header: t("common.permission"),
      render: (tool) => (
        <StatusBadge {...permissionView(tool.permission_level)} icon={false} />
      ),
    },
  ];

  return (
    <>
      <PageHeader wide title={t("nav.tools")} subtitle={t("page.tools.subtitle")} />
      <PageBody wide>
        <QueryState query={tools} loadingLabel={t("loading.tools")} skeleton={<TableSkeleton columns={2} />}>
          <AgentSplitPane
            splitId="tools-list"
            left={
              <Section title={t("tool.list")}>
                <ListToolbar
                  search={
                    <ListSearchField
                      id="tool-search"
                      label={t("tool.search")}
                      value={toolQuery}
                      onSearch={setToolQuery}
                    />
                  }
                  count={listCount(list.length, visibleTools.length, Boolean(toolQuery.trim()))}
                  testId="tool-list-toolbar"
                />
                <PagedDataTable
                  pageKey="tools"
                  resetKey={toolQuery}
                  rows={visibleTools}
                  columns={columns}
                  getRowKey={(tool) => tool.name}
                  selectedRowKey={selected?.name ?? null}
                  onRowClick={(tool) => setSelectedName(tool.name)}
                  ariaLabel={t("tool.list")}
                  empty={
                    list.length ? (
                      <NoMatchState title={t("tool.noMatch")} onClear={() => setToolQuery("")} />
                    ) : (
                      <EmptyState title={t("common.empty.title")} />
                    )
                  }
                />
              </Section>
            }
            right={
              selected ? (
                <ToolCard tool={selected} />
              ) : (
                <EmptyState title={t("common.empty.title")} hint={t("tool.selectHint")} />
              )
            }
          />
        </QueryState>
      </PageBody>
    </>
  );
}

function ToolCard({ tool }: { tool: ToolDefinition }) {
  return (
    <Card className="min-w-0">
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div className="min-w-0">
          <CardTitle>{tool.name}</CardTitle>
          <CardDescription>{tool.description}</CardDescription>
        </div>
        <StatusBadge {...permissionView(tool.permission_level)} icon={false} />
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-2">
          {tool.audit_tags.map((tag) => (
            <span key={tag} className="rounded-md border border-border px-2 py-1 text-xs text-fg-muted">
              {tag}
            </span>
          ))}
        </div>
        <div className="grid min-w-0 gap-3 md:grid-cols-2">
          <JsonPanel title={t("settings.mcpDiscovery.inputSchema")} value={tool.input_schema} />
          <JsonPanel title={t("tool.outputSchema")} value={tool.output_schema} />
        </div>
      </CardContent>
    </Card>
  );
}
