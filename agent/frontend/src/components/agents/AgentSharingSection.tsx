import { Link } from "react-router-dom";
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Send, ShieldCheck, X } from "lucide-react";
import {
  Banner,
  ButtonLink,
  Button,
  Card,
  CardContent,
  Section,
  TextareaField,
  apiErrorMessage,
  toast,
  useConfirm,
} from "@production-ready/ui";

import { agentApi, type AgentProfile } from "@/lib/api";
import { t } from "@/lib/i18n";
import { APP_ROUTES } from "@/lib/routes";
import { MENU_PERMISSIONS } from "@/lib/permissions";
import { useAuth } from "@/components/security/AuthProvider";
import { formatDate } from "@/pages/shared/page-helpers";

/** 申請の一言・却下の理由の文字数の上限（backend の `AgentShareRequestInput` / `AgentShareRejectInput`）。 */
const MAX_NOTE_CHARS = 500;

/**
 * 個人の業務 Agent の共有（#2523）。持ち主は Agent 管理に共有を申請し、Agent 管理は申請を承認・却下する。
 * 承認すると組織の業務 Agent（持ち主なし）になり、権限管理でロールに割り当てて共有する。
 */
export function AgentSharingSection({
  agent,
  mode,
  ownerName,
}: {
  agent: AgentProfile;
  /** owner = 持ち主の申請、admin = Agent 管理の承認・却下。 */
  mode: "owner" | "admin";
  ownerName: string;
}) {
  const queryClient = useQueryClient();
  const { hasPermission } = useAuth();
  const confirm = useConfirm();
  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  const [reasonError, setReasonError] = useState<string | null>(null);
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["agents"] });
  const onError = (error: unknown) => toast.error(apiErrorMessage(error, t("common.error.operation")));
  const requestShare = useMutation({
    mutationFn: () => agentApi.requestAgentShare(agent.id, note.trim()),
    onSuccess: () => {
      toast.success(t("agent.share.requested"));
      setNote("");
      refresh();
    },
    onError,
  });
  const approve = useMutation({
    mutationFn: () => agentApi.approveAgentShare(agent.id),
    onSuccess: () => {
      toast.success(t("agent.share.approved"));
      refresh();
    },
    onError,
  });
  const reject = useMutation({
    mutationFn: () => agentApi.rejectAgentShare(agent.id, reason.trim()),
    onSuccess: () => {
      toast.success(t("agent.share.rejected"));
      setReason("");
      refresh();
    },
    onError,
  });
  const request = agent.share_request ?? null;
  const requested = request?.status === "requested";

  async function confirmApprove() {
    const ok = await confirm({
      title: t("agent.share.approveTitle", { name: agent.name }),
      description: t("agent.share.approveMessage"),
      confirmLabel: t("agent.share.approve"),
      cancelLabel: t("common.cancel"),
    });
    if (ok) approve.mutate();
  }

  function submitReject() {
    if (!reason.trim()) {
      setReasonError(t("agent.share.reasonRequired"));
      return;
    }
    setReasonError(null);
    reject.mutate();
  }

  if (mode === "admin") {
    if (request?.status === "approved") return (
      <div data-testid="agent-share-approved-admin"><Banner severity="info">
        <div className="grid gap-3">
          <p>{t("agent.share.approvedNeedsAssignment")}</p>
          {hasPermission(MENU_PERMISSIONS.securityPermissions) && (
            <ButtonLink to={APP_ROUTES.securityPermissions} linkComponent={Link} icon={ShieldCheck}>
              {t("agent.share.assignRoles")}
            </ButtonLink>
          )}
        </div>
      </Banner></div>
    );
    if (!requested || !request) return null;
    return (
      <Section title={t("agent.share.reviewTitle")} description={t("agent.share.reviewDescription")}>
        <Card className="min-w-0">
          <CardContent className="space-y-3 pt-5" data-testid="agent-share-review">
            <p className="text-sm text-fg">
              {t("agent.share.reviewRequested", { name: ownerName, date: formatDate(request.requested_at) })}
            </p>
            {request.note ? (
              <p className="whitespace-pre-wrap break-words rounded-md bg-surface-sunken px-3 py-2 text-sm text-fg">
                {request.note}
              </p>
            ) : null}
            <div>
              <Button icon={Check} loading={approve.isPending} onClick={() => void confirmApprove()} data-testid="agent-share-approve">
                {t("agent.share.approve")}
              </Button>
            </div>
            <TextareaField
              id={`${agent.id}-share-reject-reason`}
              label={t("agent.share.reason")}
              helper={t("agent.share.reasonHelper")}
              width="full"
              rows={2}
              maxLength={MAX_NOTE_CHARS}
              value={reason}
              error={reasonError ?? undefined}
              onValueChange={(value) => {
                setReason(value);
                setReasonError(null);
              }}
            />
            <div>
              <Button
                variant="secondary"
                icon={X}
                loading={reject.isPending}
                onClick={submitReject}
                data-testid="agent-share-reject"
              >
                {t("agent.share.reject")}
              </Button>
            </div>
          </CardContent>
        </Card>
      </Section>
    );
  }

  const unpublished = agent.published_version === null;
  return (
    <Section title={t("agent.share.title")} description={t("agent.share.description")}>
      <Card className="min-w-0">
        <CardContent className="space-y-3 pt-5" data-testid="agent-share">
          {requested && request ? (
            <Banner severity="info">{t("agent.share.pending", { date: formatDate(request.requested_at) })}</Banner>
          ) : (
            <>
              {request?.status === "rejected" ? (
                <div data-testid="agent-share-rejected">
                  <Banner severity="warning" title={t("agent.share.rejectedTitle")}>
                    <span className="whitespace-pre-wrap break-words">{request.reason}</span>
                  </Banner>
                </div>
              ) : null}
              <TextareaField
                id={`${agent.id}-share-note`}
                label={t("agent.share.note")}
                helper={unpublished ? t("agent.share.publishFirst") : t("agent.share.noteHelper")}
                width="full"
                rows={2}
                maxLength={MAX_NOTE_CHARS}
                value={note}
                disabled={unpublished}
                onValueChange={setNote}
              />
              <div>
                <Button
                  icon={Send}
                  loading={requestShare.isPending}
                  disabled={unpublished}
                  onClick={() => requestShare.mutate()}
                  data-testid="agent-share-request"
                >
                  {t("agent.share.request")}
                </Button>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </Section>
  );
}
