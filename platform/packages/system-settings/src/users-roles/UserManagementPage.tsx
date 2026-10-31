import {
  compareNames,
  Banner,
  CopyButton,
  EmptyState,
  ErrorState,
  FormStatus,
  toast,
  DataTable,
  type DataTableColumn,
  type DataTableSort,
  StatusBadge,
  PageHeader,
  FormGrid,
  ListToolbar,
  listCountText,
  FieldActionRow,
  FieldError,
  FieldLegend,
  TextField,
  PageBody,
  useConfirm,
  ProcessingIndicator,
  ObjectActionBar,
  FormActionBar,
  cn,
  type EntityAction,
  SaveErrorBanner,
  INFORMATION_TABLE_ROW_CLASS,
  RowTitleButton,
} from "@production-ready/ui";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  KeyRound,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Trash2,
  UserCheck,
  UserRound,
  Users,
  UserX,
} from "lucide-react";

import { useUnsavedChangesGuard } from "../guards/useUnsavedChangesGuard";
import { fieldActionRowWidthClasses } from "../oci/InputActionField";
import { useRequestScope } from "../oci/useRequestScope";
import { t } from "./messages";
import { roleDisplayName } from "./RoleNameBadges";
import { userDisplayName } from "./userDisplayName";
import {
  SecurityDetailField,
  SecurityDetailFallback,
  SecurityListDetail,
  SecurityManagementPanelShell,
  SecurityPanelHeader,
  SecuritySearchField,
  SecurityClearSearchAction,
  SecurityListPagination,
  describeErrorMessageOnly,
  isAbortError,
  mapFieldErrors,
  nextUserRoleIds,
  securityListCount,
  unmappedErrorMessage,
  useFocusAfterCommit,
  usePagedSelectionList,
  useSecurityUrlSelection,
  withoutFieldError,
} from "./shared";
import {
  SYSTEM_ADMIN_ROLE_CODE,
  type AssignedRole,
  type DescribeApiError,
  type SecurityRole,
  type SecurityUser,
  type UserManagementApi,
} from "./types";

export interface UserManagementPageProps {
  api: UserManagementApi;
  /** ユーザーの作成・編集・無効化などを行えるか（製品の権限判定）。 */
  canManage: boolean;
  /** ログイン中のユーザー。自分自身は削除できない。 */
  currentUserUuid?: string | null;
  /** 製品の API エラーから入力項目のエラーとエラーコードを取り出す。 */
  describeError?: DescribeApiError;
  /** 一覧 + 詳細（ListDetailLayout）の一覧の幅と開閉を保存する localStorage key の前置き（製品ごと。#2282）。 */
  listDetailStoragePrefix?: string;
}

/** ユーザー管理（3製品共通。NL2SQL の画面を移設。#206）。 */
type UserPanelView = "list" | "create" | "edit";

interface ResetPasswordError {
  userUuid: string;
  message: string;
}

interface UserDraftState {
  loginUserId: string;
  displayName: string;
  selectedRoleId: string;
  temporaryPassword: string;
}

type UserFormField = keyof UserDraftState;
type UserFieldErrors = Partial<Record<UserFormField, string>>;

const USER_POINTER_TO_FIELD = {
  "/login_user_id": "loginUserId",
  "/display_name": "displayName",
  "/role_ids": "selectedRoleId",
  "/temporary_password": "temporaryPassword",
} as const satisfies Readonly<Record<string, UserFormField>>;

const EMPTY_DRAFT: UserDraftState = {
  loginUserId: "",
  displayName: "",
  selectedRoleId: "",
  temporaryPassword: "",
};

function compareText(left: string, right: string, direction: DataTableSort["direction"]) {
  return compareNames(left, right, direction);
}

function assignedRoleLabel(role: AssignedRole) {
  // ロール名で出す。参照できない（削除済みの）ロールは ID（UUID）を出さない（#1682）。
  const name = roleDisplayName(role);
  if (role.deleted || name === roleDisplayName(null)) return name;
  return role.archived ? t("security.users.archivedRoleLabel", { role: name }) : name;
}

function userStatusLabel(user: SecurityUser) {
  return user.status === "ACTIVE" ? t("security.common.active") : t("security.common.disabled");
}

export function UserManagementPage({
  api,
  canManage,
  currentUserUuid = null,
  describeError = describeErrorMessageOnly,
  listDetailStoragePrefix,
}: UserManagementPageProps) {
  const confirm = useConfirm();
  const [users, setUsers] = useState<SecurityUser[]>([]);
  const [roles, setRoles] = useState<SecurityRole[]>([]);
  const [loading, setLoading] = useState(true);
  // 「最新情報を取得」を押した読み直しの間だけ、そのボタンを回す。初回の読込・エラーからの再試行は
  // 押していないので、一覧の読込表示がスピナーを出し、「最新情報を取得」は disabled だけにする（#819）。
  const [refreshRequested, setRefreshRequested] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [formError, setFormError] = useState("");
  const [fieldErrors, setFieldErrors] = useState<UserFieldErrors>({});
  // 選択中の利用者は URL の `?user=<user_uuid>` が唯一の情報源（#2282）。先頭の行を自動で選ばない。
  // 一覧から選ぶときは select（1 ペインでは履歴に積む）、操作の結果で選び直すときは replace。
  const selection = useSecurityUrlSelection("user");
  const selectedUserId = selection.selectedId;
  const [editingId, setEditingId] = useState<string | null>(null);
  const [activeView, setActiveView] = useState<UserPanelView>("list");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<DataTableSort>({ key: "user", direction: "asc" });
  const [draft, setDraft] = useState<UserDraftState>(EMPTY_DRAFT);
  const [baseline, setBaseline] = useState<UserDraftState>(EMPTY_DRAFT);
  const [unlockingUserId, setUnlockingUserId] = useState<string | null>(null);
  const [copyPasswordError, setCopyPasswordError] = useState("");
  const [resettingUserId, setResettingUserId] = useState<string | null>(null);
  const [statusChangingUserId, setStatusChangingUserId] = useState<string | null>(null);
  const [deletingUserId, setDeletingUserId] = useState<string | null>(null);
  const [resetPasswordError, setResetPasswordError] = useState<ResetPasswordError | null>(null);
  const formRef = useRef<HTMLFormElement | null>(null);
  const loadSequence = useRef(0);
  const loginUserIdRef = useRef<HTMLInputElement | null>(null);
  const displayNameRef = useRef<HTMLInputElement | null>(null);
  const temporaryPasswordRef = useRef<HTMLInputElement | null>(null);
  const roleGroupRef = useRef<HTMLDivElement | null>(null);
  const { abortAll, run: runScopedRequest } = useRequestScope();

  const roleById = useMemo(
    () => new Map(roles.map((role) => [role.role_id, role])),
    [roles]
  );
  const systemAdminRoleId = useMemo(
    () => roles.find((role) => role.role_code === SYSTEM_ADMIN_ROLE_CODE)?.role_id ?? null,
    [roles]
  );

  const editingUser = users.find((user) => user.user_uuid === editingId) ?? null;
  // 一時パスワードの欄の操作（コピー・再発行）の失敗。欄の下に出し、欄の aria-describedby で結び付ける。
  const temporaryPasswordActionError =
    copyPasswordError ||
    (resetPasswordError?.userUuid === editingUser?.user_uuid ? (resetPasswordError?.message ?? "") : "");
  // 一時パスワードの欄（md）とコピーの操作の行の幅（#1891）。
  const temporaryPasswordRowWidth = fieldActionRowWidthClasses("md");
  const userFormReadOnly = activeView === "edit" && editingUser?.status !== "ACTIVE";
  const canSubmitUserForm = !userFormReadOnly;
  const accountActionBusy =
    resettingUserId !== null || statusChangingUserId !== null || deletingUserId !== null || unlockingUserId !== null;
  const operationBusy = busy || accountActionBusy || loading;
  // 保存を試みた回数（保存の失敗の Banner を、同じ文言の失敗でも入れ直す。#585）。
  const [submitAttempt, setSubmitAttempt] = useState(0);
  const inputReadOnly = userFormReadOnly || operationBusy;
  const isDirty = activeView !== "list" && (
    draft.loginUserId !== baseline.loginUserId || draft.displayName !== baseline.displayName ||
    draft.selectedRoleId !== baseline.selectedRoleId ||
    (activeView === "create" && draft.temporaryPassword !== baseline.temporaryPassword)
  );
  const confirmLeave = async () => !operationBusy && (!isDirty || await confirm({
    title: t("security.common.discardTitle"),
    description: t("security.common.discardDescription"),
    confirmLabel: t("security.common.discardConfirm"),
    tone: "danger",
    dismissOnOverlay: false,
  }));
  useUnsavedChangesGuard(isDirty || busy || accountActionBusy, confirmLeave);
  const assignedRoles = useCallback((user: SecurityUser): AssignedRole[] => {
    if (user.assigned_roles?.length) return user.assigned_roles;
    return user.role_ids.map((id) => {
      const role = roleById.get(id);
      return role
        ? {
            role_id: role.role_id,
            role_code: role.role_code,
            display_name: role.display_name,
            is_built_in: role.is_built_in,
            archived: role.archived,
          }
        : {
            // 一覧に無いロール。ID を名前の欄に入れない（#1682）。
            role_id: id,
            role_code: "",
            display_name: "",
            is_built_in: false,
            archived: true,
            deleted: true,
          };
    });
  }, [roleById]);
  // 一覧の検索・並べ替え（useMemo）から使うため、roleById が変わったときだけ作り直す。
  const roleSummary = useCallback((user: SecurityUser) => {
    const names = assignedRoles(user).map(assignedRoleLabel);
    return names.length > 0 ? names.join(", ") : t("security.common.none");
  }, [assignedRoles]);
  const isSystemAdminRole = (role: SecurityRole) =>
    role.role_code === SYSTEM_ADMIN_ROLE_CODE || role.role_id === systemAdminRoleId;
  // SYSTEM_ADMIN は構成管理者（.env の system_admin）だけが持ち、DB のユーザーには割り当てない。
  // 新規作成・編集のどちらでも選択肢に出さない（#1792）。
  const roleOptions = roles.filter((role) => !isSystemAdminRole(role));
  const systemAdminRoleHidden = roleOptions.length < roles.length;
  const selectKnownRoleId = (roleIds: string[]) => {
    // 選択肢に無い SYSTEM_ADMIN は選ばない（「ほかに割り当てられているロール」に出る）。
    const availableRoleIds = new Set(roleOptions.map((role) => role.role_id));
    return roleIds.find((roleId) => availableRoleIds.has(roleId)) ?? "";
  };

  const filteredUsers = useMemo(() => {
    const q = search.trim().toLowerCase();
    return users
      .filter((user) => {
        if (!q) return true;
        return (
          user.display_name.toLowerCase().includes(q) ||
          user.login_user_id.toLowerCase().includes(q) ||
          user.status.toLowerCase().includes(q) ||
          roleSummary(user).toLowerCase().includes(q)
        );
      })
      .sort((left, right) => {
        if (sort.key === "roles") return compareText(roleSummary(left), roleSummary(right), sort.direction);
        if (sort.key === "status") return compareText(userStatusLabel(left), userStatusLabel(right), sort.direction);
        // 1 列目は表示名を出すため、表示名で並べる（#1696）。
        return compareText(userDisplayName(left), userDisplayName(right), sort.direction);
      });
  }, [roleSummary, search, sort, users]);

  // 一覧は 25 件ずつのページ（表示件数は選べる）。検索語・並べ替えが変わったら 1 ページ目へ戻す（#1891）。
  const userList = usePagedSelectionList({
    items: filteredUsers,
    getKey: (user) => user.user_uuid,
    selectedId: selectedUserId,
    active: activeView === "list",
    ready: !loading,
    resetKey: `${search}\u0000${sort.key}\u0000${sort.direction}`,
    pagingKey: "users",
  });
  const userListCount = securityListCount(users.length, filteredUsers.length, search);
  const selectedUser = users.find((user) => user.user_uuid === selectedUserId) ?? null;

  const load = async (announce = false) => {
    if (busy || accountActionBusy) return;
    const sequence = loadSequence.current + 1;
    loadSequence.current = sequence;
    setLoading(true);
    setRefreshRequested(announce);
    setLoadError("");
    setActionError("");
    await requestData(sequence, announce);
  };

  // 製品が描画のたびに新しい api オブジェクトを渡しても初回読込をやり直さないよう、最新の api は ref で読む。
  const apiRef = useRef(api);
  useLayoutEffect(() => {
    apiRef.current = api;
  });

  // 読込中・エラー表示の初期化は呼び出し側で行う（初回表示は初期 state が読込中）。
  // 初回読込の effect から使うため、安定した参照にする（依存は useRequestScope の安定した run だけ）。
  const requestData = useCallback(async (sequence: number, announce: boolean) => {
    try {
      await runScopedRequest(async (signal) => {
        const [userRows, roleRows] = await Promise.all([
          apiRef.current.users({ signal }),
          apiRef.current.roles(false, { signal }),
        ]);
        if (signal.aborted || sequence !== loadSequence.current) return;
        setUsers(userRows);
        setRoles(roleRows.filter((role) => !role.archived));
      });
      if (announce && sequence === loadSequence.current) {
        toast.success(t("common.action.refreshed"));
      }
    } catch (cause) {
      if (isAbortError(cause)) {
        return;
      }
      const nextError =
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : t("security.common.loadError");
      if (sequence === loadSequence.current) setLoadError(nextError);
    } finally {
      if (sequence === loadSequence.current) setLoading(false);
    }
  }, [runScopedRequest]);

  // abortAll / requestData は安定した参照なので、この effect はマウント時に 1 回だけ動く。
  useEffect(() => {
    const sequence = loadSequence.current + 1;
    loadSequence.current = sequence;
    void requestData(sequence, false);
    return () => {
      loadSequence.current += 1;
      abortAll();
    };
  }, [abortAll, requestData]);

  const clearFieldError = (field: UserFormField) => {
    setFieldErrors((current) => withoutFieldError(current, field));
    setFormError("");
  };

  const updateDraftField = <Field extends UserFormField>(
    field: Field,
    value: UserDraftState[Field]
  ) => {
    if (userFormReadOnly) return;
    setDraft((current) => ({ ...current, [field]: value }));
    clearFieldError(field);
  };

  // 送信中は欄が disabled のため、操作できる状態を commit した後に移す（#424）。
  const scheduleFocus = useFocusAfterCommit(!inputReadOnly);
  const focusFirstFieldError = (errors: UserFieldErrors) => {
    scheduleFocus(() => {
      if (errors.loginUserId) loginUserIdRef.current?.focus();
      else if (errors.displayName) displayNameRef.current?.focus();
      else if (errors.temporaryPassword) temporaryPasswordRef.current?.focus();
      else if (errors.selectedRoleId) {
        roleGroupRef.current
          ?.querySelector<HTMLInputElement>('input[type="radio"]:not(:disabled)')
          ?.focus();
      }
    });
  };

  const startCreate = () => {
    setActiveView("create");
    setEditingId(null);
    setDraft(EMPTY_DRAFT);
    setBaseline(EMPTY_DRAFT);
    setFormError("");
    setFieldErrors({});
    setActionError("");
    setCopyPasswordError("");
    setResetPasswordError(null);
  };

  const startEdit = (user: SecurityUser, temporaryPassword = "") => {
    selection.replace(user.user_uuid);
    setEditingId(user.user_uuid);
    setActiveView("edit");
    const nextDraft = {
      loginUserId: user.login_user_id,
      displayName: user.display_name,
      selectedRoleId: selectKnownRoleId(user.role_ids),
      temporaryPassword,
    };
    setDraft(nextDraft);
    setBaseline(nextDraft);
    setFormError("");
    setFieldErrors({});
    setActionError("");
    setCopyPasswordError("");
    setResetPasswordError(null);
  };

  const returnToList = async () => {
    if (!(await confirmLeave())) return;
    setDraft(EMPTY_DRAFT);
    setBaseline(EMPTY_DRAFT);
    setActiveView("list");
    setEditingId(null);
    setFormError("");
    setFieldErrors({});
    setActionError("");
    setCopyPasswordError("");
    setResetPasswordError(null);
  };

  // 変更を破棄: 入力を開いたときの値に戻す（#1786。変更が無いときは disabled）。
  const discardChanges = () => {
    setDraft(baseline);
    setFieldErrors({});
    setFormError("");
  };

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (operationBusy || !canSubmitUserForm) return;
    setSubmitAttempt((current) => current + 1);
    setFormError("");
    setFieldErrors({});
    setActionError("");
    // 未入力は送信前に欄の下へ出し、最初のエラーの欄へフォーカスする（noValidate。#531）。
    const requiredErrors: UserFieldErrors = {};
    if (activeView === "create" && !draft.loginUserId.trim()) {
      requiredErrors.loginUserId = t("security.users.loginUserIdRequired");
    }
    if (!draft.displayName.trim()) {
      requiredErrors.displayName = t("security.users.displayNameRequired");
    }
    if (!draft.selectedRoleId) {
      requiredErrors.selectedRoleId = t("security.users.roleRequired");
    }
    if (Object.keys(requiredErrors).length > 0) {
      setFieldErrors(requiredErrors);
      focusFirstFieldError(requiredErrors);
      return;
    }
    const submittedRoleId = draft.selectedRoleId;
    setBusy(true);
    try {
      if (activeView === "edit") {
        if (!editingUser) return;
        // 選び直したロールだけを置き換え、選択欄に出ないほかのロールは残す（#1050）。
        const updated = await api.updateUser({
          ...editingUser,
          display_name: draft.displayName,
          role_ids: nextUserRoleIds(editingUser.role_ids, baseline.selectedRoleId, submittedRoleId),
        });
        setUsers((rows) => rows.map((row) => (row.user_uuid === updated.user_uuid ? updated : row)));
        // 応答のロールの並びは選んだ順と限らないため、保存したロールが残っていればそれを選択のままにする。
        const savedRoleId = updated.role_ids.includes(submittedRoleId)
          ? submittedRoleId
          : selectKnownRoleId(updated.role_ids);
        setBaseline((current) => ({
          ...current,
          displayName: updated.display_name,
          selectedRoleId: savedRoleId,
        }));
        setDraft((current) => ({
          ...current,
          displayName: updated.display_name,
          selectedRoleId: savedRoleId,
        }));
        toast.success(t("security.common.saved"));
      } else {
        const created = await api.createUser({
          login_user_id: draft.loginUserId,
          display_name: draft.displayName,
          role_ids: [submittedRoleId],
          temporary_password: draft.temporaryPassword || undefined,
        });
        setUsers((rows) => [...rows, created.user]);
        startEdit(created.user, created.temporary_password);
        toast.success(t("security.common.saved"));
      }
    } catch (cause) {
      const details = describeError(cause);
      const nextErrors = mapFieldErrors(
        details,
        USER_POINTER_TO_FIELD,
        (problem, problemDetails) =>
          problemDetails.code === "SECURITY_USER_LOGIN_ID_CONFLICT" &&
          problem.pointer === "/login_user_id"
            ? t("security.users.loginUserIdConflict")
            : problem.message
      );
      setFieldErrors(nextErrors);
      setFormError(
        unmappedErrorMessage(cause, details, USER_POINTER_TO_FIELD, t("security.common.saveError"))
      );
      if (Object.keys(nextErrors).length > 0) {
        focusFirstFieldError(nextErrors);
      }
    } finally {
      setBusy(false);
    }
  };

  const handleToggleStatus = async (user: SecurityUser) => {
    if (operationBusy) return;
    const enabling = user.status !== "ACTIVE";
    if (
      !enabling &&
      !(await confirm({
        title: t("security.users.disable"),
        description: t("security.users.disableConfirm"),
        tone: "danger",
      }))
    ) {
      return;
    }
    setActionError("");
    setResetPasswordError(null);
    setStatusChangingUserId(user.user_uuid);
    try {
      const updated = await api.setUserEnabled(user, enabling);
      setUsers((rows) => rows.map((row) => (row.user_uuid === updated.user_uuid ? updated : row)));
      toast.success(
        t(enabling ? "security.users.enableSuccess" : "security.users.disableSuccess")
      );
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : t("security.common.saveError"));
    } finally {
      setStatusChangingUserId(null);
    }
  };

  const handleResetPassword = async (user: SecurityUser) => {
    if (user.status !== "ACTIVE" || operationBusy) return;
    if (
      !(await confirm({
        title: t("security.users.resetPassword"),
        description: t("security.users.resetConfirm"),
        tone: "warning",
      }))
    ) {
      return;
    }
    setActionError("");
    setCopyPasswordError("");
    setResetPasswordError(null);
    setResettingUserId(user.user_uuid);
    try {
      const result = await api.resetPassword(user.user_uuid);
      setUsers((rows) => rows.map((row) => (row.user_uuid === result.user.user_uuid ? result.user : row)));
      if (activeView === "edit" && editingId === result.user.user_uuid) {
        setDraft((current) => ({
          ...current,
          temporaryPassword: result.temporary_password,
        }));
      } else {
        startEdit(result.user, result.temporary_password);
      }
      toast.success(t("security.users.oneTimePassword.resetSuccess"));
    } catch (cause) {
      setResetPasswordError({
        userUuid: user.user_uuid,
        message:
          cause instanceof Error && cause.message.trim()
            ? cause.message
            : t("security.users.oneTimePassword.resetError"),
      });
    } finally {
      setResettingUserId(null);
    }
  };

  const handleUnlock = async (user: SecurityUser) => {
    if (operationBusy) return;
    setUnlockingUserId(user.user_uuid);
    setActionError("");
    try {
      const updated = await api.unlockUser(user.user_uuid);
      setUsers((rows) => rows.map((row) => (row.user_uuid === updated.user_uuid ? updated : row)));
      toast.success(t("security.common.saved"));
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : t("security.common.saveError"));
    } finally {
      setUnlockingUserId(null);
    }
  };

  const canDeleteUser = (user: SecurityUser) =>
    user.status === "DISABLED" &&
    !user.is_bootstrap_admin &&
    currentUserUuid !== user.user_uuid;

  const handleDelete = async (user: SecurityUser) => {
    if (operationBusy || !canDeleteUser(user)) return;
    if (
      !(await confirm({
        title: t("security.users.delete"),
        description: t("security.users.deleteConfirm", { name: userDisplayName(user) }),
        confirmLabel: t("common.delete"),
        tone: "danger",
        dismissOnOverlay: false,
      }))
    ) {
      return;
    }

    setActionError("");
    setResetPasswordError(null);
    setDeletingUserId(user.user_uuid);
    try {
      await api.deleteUser(user);
      setUsers((rows) => rows.filter((row) => row.user_uuid !== user.user_uuid));
      // 削除した対象の選択を外す（ほかの行へ置き換えない。page-archetypes.md §1 B。#2282）。
      selection.replace(null);
      setEditingId(null);
      setActiveView("list");
      setFormError("");
      setFieldErrors({});
      setCopyPasswordError("");
      setResetPasswordError(null);
      toast.success(t("security.users.deleteSuccess", { name: userDisplayName(user) }));
    } catch (cause) {
      setActionError(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : t("security.users.deleteError")
      );
    } finally {
      setDeletingUserId(null);
    }
  };

  const resetPasswordAction = (user: SecurityUser): EntityAction => ({
    id: "reset-password",
    label: t("security.users.resetPassword"),
    icon: KeyRound,
    onSelect: () => handleResetPassword(user),
    visible: user.status === "ACTIVE" && !user.is_bootstrap_admin,
    loading: resettingUserId === user.user_uuid,
    // 押したボタンは loading（aria-disabled）でフォーカスを保つ。ネイティブの disabled にしない（#355 / #835）。
    disabled: operationBusy && !(resettingUserId === user.user_uuid),
  });

  const userActions = (user: SecurityUser): EntityAction[] =>
    canManage
      ? [
          {
            id: "edit",
            label: t("security.common.edit"),
            icon: Pencil,
            disabled: operationBusy,
            onSelect: () => { if (!operationBusy) startEdit(user); },
          },
          resetPasswordAction(user),
          {
            id: "unlock",
            label: t("security.users.unlock"),
            visible: Boolean(user.locked_until),
            loading: unlockingUserId === user.user_uuid,
            // 押したボタンは loading（aria-disabled）でフォーカスを保つ。ネイティブの disabled にしない（#355 / #835）。
            disabled: operationBusy && !(unlockingUserId === user.user_uuid),
            onSelect: () => handleUnlock(user),
          },
          {
            id: user.status === "ACTIVE" ? "disable" : "enable",
            label: user.status === "ACTIVE" ? t("security.users.disable") : t("security.users.enable"),
            icon: user.status === "ACTIVE" ? UserX : UserCheck,
            tone: user.status === "ACTIVE" ? "danger" : "default",
            loading: statusChangingUserId === user.user_uuid,
            // 押したボタンは loading（aria-disabled）でフォーカスを保つ。ネイティブの disabled にしない（#355 / #835）。
            disabled: operationBusy && !(statusChangingUserId === user.user_uuid),
            onSelect: () => handleToggleStatus(user),
          },
          {
            id: "delete",
            label: t("security.users.delete"),
            icon: Trash2,
            tone: "danger",
            visible: canDeleteUser(user),
            loading: deletingUserId === user.user_uuid,
            // 押したボタンは loading（aria-disabled）でフォーカスを保つ。ネイティブの disabled にしない（#355 / #835）。
            disabled: operationBusy && !(deletingUserId === user.user_uuid),
            onSelect: () => handleDelete(user),
          },
        ]
      : [];

  /**
   * 編集フォームの対象への操作（パスワードのリセット・有効化・無効化・削除）。フォームのパネルの見出しの右の
   * ObjectActionBar 1 か所に置く（#618）。保存はページの右上（PageHeader）の primary。
   */
  const editObjectActions = (): EntityAction[] => {
    if (!editingUser || !canManage) return [];
    const ids = new Set(["reset-password", "enable", "disable", "delete"]);
    return userActions(editingUser).filter((action) => ids.has(action.id));
  };

  const selectRole = (roleId: string) => {
    if (userFormReadOnly) return;
    updateDraftField("selectedRoleId", roleId);
  };

  // 一覧の列は狭い（既定 20rem）ので、名前と補足 1 行（割り当てロール）と状態だけを出す（#2282）。
  // ログイン ID は詳細の「ログインユーザーID」の項目に出す（#1696）。
  const userColumns: Array<DataTableColumn<SecurityUser>> = [
    {
      key: "user",
      header: t("security.users.column.user"),
      sortable: true,
      className: "align-top",
      render: (user) => (
        <RowTitleButton
          title={userDisplayName(user)}
          subtitle={<span className="line-clamp-1 break-all">{roleSummary(user)}</span>}
          current={selectedUserId === user.user_uuid}
          aria-label={t("security.users.showUser", { name: userDisplayName(user) })}
          onClick={(event) => {
            event.stopPropagation();
            if (operationBusy) return;
            selection.select(user.user_uuid);
          }}
        />
      ),
    },
    {
      key: "status",
      header: t("security.common.status"),
      sortable: true,
      headerClassName: "w-[8rem]",
      className: "align-top",
      render: (user) => <UserStatusBadges user={user} />,
    },
  ];

  const initialLoadFailed = Boolean(loadError) && users.length === 0 && roles.length === 0;
  // 編集中のユーザーの、選択欄で選ばれていないほかのロール（保存しても変わらない。#1050）。
  const otherAssignedRoles =
    activeView === "edit" && editingUser
      ? assignedRoles(editingUser).filter(
          (role) => role.role_id !== baseline.selectedRoleId && role.role_id !== draft.selectedRoleId,
        )
      : [];

  return (
    <>
      <PageHeader wide
        title={t("nav.securityUsers")}
        subtitle={t("security.users.subtitle")}
        actions={
          activeView === "list"
            ? [
                ...(canManage && !initialLoadFailed
                  ? [
                      {
                        id: "create-user",
                        kind: "primary" as const,
                        label: t("security.common.create"),
                        icon: Plus,
                        disabled: operationBusy,
                        onClick: startCreate,
                      },
                    ]
                  : []),
                {
                  id: "refresh",
                  kind: "utility",
                  label: t("common.action.refresh"),
                  icon: RefreshCw,
                  // 押したボタンは loading（aria-disabled）でフォーカスを保つ。ネイティブの disabled にしない（#355 / #835）。
                  disabled: operationBusy && !(loading && refreshRequested),
                  onClick: () => load(true),
                  loading: loading && refreshRequested,
                },
              ]
            : []
        }
        // 詳細・作成・編集の画面の「一覧へ戻る」は左上（#618）。確定（作成・保存）はフォームの末尾の FormActionBar（#1786）。
        // 一覧 + 詳細の 1 ペイン（xl 未満）で詳細を出している間も、左上の「一覧へ戻る」で一覧へ戻る（#2282）。
        back={
          activeView !== "list"
            ? { label: t("security.common.backToList"), onClick: returnToList, disabled: operationBusy, testId: "security-users-back" }
            : !selection.split && selectedUserId
              ? { label: t("security.common.backToList"), onClick: selection.clear, testId: "security-users-back" }
              : undefined
        }
        actionsLabel={t("security.users.actionsLabel")}
        actionsTestId="security-users-actions"
      />
      <PageBody wide className="grid gap-4">
        {loadError && !initialLoadFailed ? <Banner severity="danger">{loadError}</Banner> : null}
        {activeView === "list" && actionError ? (
          <Banner severity="danger">{actionError}</Banner>
        ) : null}

        {initialLoadFailed ? (
          <ErrorState message={loadError} onRetry={() => void load()} />
        ) : activeView === "list" ? (
          // 一覧は狭い列・詳細は広い列（xl 未満は 1 ペイン）。選択は URL の ?user=（#2282）。
          <SecurityListDetail
            id="security-users"
            storagePrefix={listDetailStoragePrefix}
            icon={Users}
            headingId="security-users-list-heading"
            listLabel={t("security.users.list")}
            detailLabel={t("security.users.detail")}
            toolbar={
              // 件数はツールバーの右の ListCount 1 か所（見出しのバッジに出さない。#1891）。件数の読み上げは
              // 検索欄の resultCountLabel が行うので、ListCount は読み上げない（二重にしない）。
              <ListToolbar
                testId="security-users-toolbar"
                search={
                  <SecuritySearchField
                    label={t("security.common.search")}
                    placeholder={t("security.users.searchPlaceholder")}
                    value={search}
                    testId="security-users-search"
                    disabled={operationBusy}
                    resultCountLabel={listCountText(userListCount)}
                    onChange={setSearch}
                  />
                }
                count={{ ...userListCount, live: false, testId: "security-users-count" }}
              />
            }
            loadingIndicator={
              loading ? (
                <ProcessingIndicator
                  active
                  label={t("security.users.loading")}
                  operationKey="security-users-load"
                  placement="panel"
                  testId="security-users-loading"
                  activityIcon={refreshRequested ? "none" : "spinner"}
                />
              ) : null
            }
            list={
              <DataTable
                dense
                loading={loading}
                rows={userList.pageItems}
                sort={sort}
                onSortChange={(next) => { if (!operationBusy) setSort(next); }}
                selectedRowKey={selectedUserId}
                onRowClick={(user) => {
                  if (operationBusy) return;
                  selection.select(user.user_uuid);
                }}
                getRowKey={(user) => user.user_uuid}
                rowProps={(user) => ({ className: INFORMATION_TABLE_ROW_CLASS, "aria-label": t("security.users.showUser", { name: userDisplayName(user) }) })}
                ariaLabel={t("security.users.list")}
                testId="security-users-grid"
                scrollAriaLabel={t("security.common.listScrollLabel", {
                  list: t("security.users.list"),
                })}
                scrollTestId="security-users-scroll-region"
                // 一覧の列（sticky・画面の高さまで）の中で縦にスクロールし、表頭を固定する。列の枠と二重にしない。
                className="min-h-0 rounded-none border-0"
                tableClassName="w-full table-fixed"
                stickyHeader
                empty={<EmptyState title={search ? t("security.users.noResultsTitle") : t("security.common.empty")} hint={search ? t("security.users.noResultsHint") : undefined} action={search ? <SecurityClearSearchAction onClear={() => setSearch("")} /> : undefined} />}
                columns={userColumns}
              />
            }
            footer={
              <SecurityListPagination
                list={userList}
                ariaLabel={t("security.users.pagination")}
                testId="security-users-pagination"
                disabled={operationBusy}
              />
            }
            pageItems={userList.pageItems}
            getKey={(user) => user.user_uuid}
            selectedId={selectedUserId}
            onSelect={selection.replace}
            disabled={operationBusy}
            emptyTitle={t("security.users.noSelectionTitle")}
            emptyHint={t("security.users.noSelectionHint")}
            detail={
              selectedUser ? (
                <UserDetailPanel
                  user={selectedUser}
                  canManage={canManage}
                  assignedRoles={assignedRoles(selectedUser)}
                  actions={userActions(selectedUser)}
                  resetError={
                    resetPasswordError?.userUuid === selectedUser.user_uuid ? resetPasswordError.message : ""
                  }
                />
              ) : (
                <SecurityDetailFallback
                  loading={loading}
                  title={t("security.users.notFoundTitle")}
                  hint={t("security.users.notFoundHint")}
                  testId="security-users-detail-not-found"
                />
              )
            }
          />
        ) : (
          <>
            <SecurityManagementPanelShell
              id={`security-users-panel-${activeView}`}
              idPrefix="security-users"
              ariaLabel={t("security.users.taskPanelLabel")}
            >
              <SecurityPanelHeader
                icon={activeView === "create" ? Plus : Pencil}
                title={activeView === "edit" ? t("security.users.form.edit") : t("security.users.form.create")}
                description={t("security.users.formHint")}
                headingId="security-users-form-heading"
                action={
                  activeView === "edit" ? (
                    <ObjectActionBar
                      actions={editObjectActions()}
                      ariaLabel={t("security.users.editActions")}
                      testId="security-users-object-actions"
                    />
                  ) : undefined
                }
              />
              <form
                ref={formRef}
                className="grid gap-4"
                onSubmit={handleSubmit}
                aria-labelledby="security-users-form-heading"
                noValidate
              >
                {/* 欄の並びは FormGrid（最大 60rem・置き場所 42rem 以上で 2 列）。幅は値の種類の段（#1891）。 */}
                <FormGrid>
                  <TextField
                    ref={loginUserIdRef}
                    id="security-user-login-user-id"
                    label={t("security.users.loginUserId")}
                    required
                    maxLength={64}
                    width="md"
                    disabled={activeView === "edit" || operationBusy}
                    error={fieldErrors.loginUserId}
                    autoComplete="off"
                    value={draft.loginUserId}
                    onValueChange={(value) => updateDraftField("loginUserId", value)}
                  />
                  <TextField
                    ref={displayNameRef}
                    id="security-user-display-name"
                    label={t("security.users.displayName")}
                    required
                    width="md"
                    disabled={inputReadOnly}
                    error={fieldErrors.displayName}
                    value={draft.displayName}
                    onValueChange={(value) => updateDraftField("displayName", value)}
                  />
                    {/* 一時パスワード。作成では入力、編集では発行した値（読み取り専用）とコピーの操作の行。
                        エラーは操作の下端がずれないよう footer に出す（#613 / #631）。欄は md の段で、
                        コピーの操作は欄のすぐ右（#1891）。 */}
                    <FieldActionRow
                      className={temporaryPasswordRowWidth.row}
                      fit={temporaryPasswordRowWidth.fit}
                      actions={
                        activeView === "edit" ? (
                          <CopyButton
                            text={draft.temporaryPassword}
                            label={t("security.users.oneTimePassword.copy")}
                            copiedMessage={t("common.action.copied")}
                            failedMessage={t("security.users.oneTimePassword.copyError")}
                            disabled={userFormReadOnly || !draft.temporaryPassword}
                            onCopied={() => setCopyPasswordError("")}
                            onCopyError={setCopyPasswordError}
                            testId="security-user-temporary-password-copy"
                          />
                        ) : null
                      }
                      footer={
                        fieldErrors.temporaryPassword || activeView === "edit" ? (
                        <div className={cn("space-y-1.5", temporaryPasswordRowWidth.footer)}>
                          <FieldError
                            id="security-user-temporary-password-error"
                            message={fieldErrors.temporaryPassword}
                          />
                          {activeView === "edit" ? (
                            <div
                              id="security-user-temporary-password-action-error"
                              data-testid="security-user-temporary-password-error"
                            >
                              <FormStatus tone="danger" message={temporaryPasswordActionError} />
                            </div>
                          ) : null}
                        </div>
                        ) : null
                      }
                    >
                      <TextField
                        ref={temporaryPasswordRef}
                        id="security-user-temporary-password"
                        label={t(
                          activeView === "create"
                            ? "security.users.tempPassword"
                            : "security.users.oneTimePassword.valueLabel"
                        )}
                        type={activeView === "create" ? "password" : "text"}
                        width="md"
                        readOnly={activeView === "edit"}
                        disabled={inputReadOnly}
                        aria-invalid={Boolean(fieldErrors.temporaryPassword)}
                        aria-describedby={
                          fieldErrors.temporaryPassword
                            ? "security-user-temporary-password-error"
                            : activeView === "edit" && temporaryPasswordActionError
                              ? "security-user-temporary-password-action-error"
                              : undefined
                        }
                        inputClassName={cn(fieldErrors.temporaryPassword && "border-danger-fg")}
                        autoComplete={activeView === "create" ? "new-password" : "off"}
                        value={draft.temporaryPassword}
                        onValueChange={(value) => {
                          if (activeView === "create") {
                            updateDraftField("temporaryPassword", value);
                          }
                        }}
                        data-testid="security-user-temporary-password"
                      />
                    </FieldActionRow>
                </FormGrid>
                    <fieldset className="grid gap-2" disabled={inputReadOnly}>
                  {/* 必須は下の radiogroup の aria-required で伝えるので、legend のタグは読み上げない。 */}
                  <FieldLegend id="security-users-role-legend" required requiredAnnouncedByControl>
                    {t("security.users.roles")}
                  </FieldLegend>
                  {roleOptions.length === 0 ? (
                    <p className="text-sm text-fg-muted">{t("security.users.noRole")}</p>
                  ) : (
                    <div
                      ref={roleGroupRef}
                      className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3"
                      role="radiogroup"
                      aria-labelledby="security-users-role-legend"
                      aria-required="true"
                      aria-invalid={fieldErrors.selectedRoleId ? "true" : undefined}
                      aria-describedby={
                        [
                          systemAdminRoleHidden ? "security-users-role-system-admin-note" : "",
                          fieldErrors.selectedRoleId ? "security-users-role-error" : "",
                        ].filter(Boolean).join(" ") || undefined
                      }
                    >
                      {roleOptions.map((role) => {
                        const disabled = userFormReadOnly;
                        const selected = draft.selectedRoleId === role.role_id;
                        return (
                          <label
                            key={role.role_id}
                            data-surface-tint={disabled ? "neutral" : selected ? "info" : undefined}
                            className={cn(
                              "flex min-h-11 items-start gap-2 rounded-md border p-2.5 text-sm transition-colors",
                              disabled
                                ? "cursor-not-allowed bg-surface-hover text-fg-muted"
                                : selected
                                  ? "cursor-pointer border-accent-emphasis bg-info-subtle"
                                  : "cursor-pointer border-border hover:bg-surface-hover"
                            )}
                          >
                            <input
                              className="mt-0.5 h-4 w-4 accent-accent-emphasis disabled:cursor-not-allowed"
                              type="radio"
                              name="security-users-role"
                              value={role.role_id}
                              checked={selected}
                              disabled={disabled}
                              onChange={() => selectRole(role.role_id)}
                            />
                            <span className="min-w-0">
                              {/* ロール名だけを出す（コード・ID は出さない。#1682） */}
                              <span className="block break-words font-medium">{roleDisplayName(role)}</span>
                            </span>
                          </label>
                        );
                      })}
                    </div>
                  )}
                  {systemAdminRoleHidden ? (
                    <p
                      id="security-users-role-system-admin-note"
                      className="text-sm text-fg-muted"
                      data-testid="security-users-role-system-admin-note"
                    >
                      {t("security.users.systemAdminConfigOnly")}
                    </p>
                  ) : null}
                  <FieldError id="security-users-role-error" message={fieldErrors.selectedRoleId} />
                  {otherAssignedRoles.length > 0 ? (
                    <p className="text-sm text-fg-muted" data-testid="security-users-other-roles">
                      {t("security.users.otherRoles", {
                        roles: otherAssignedRoles.map(assignedRoleLabel).join(", "),
                      })}
                    </p>
                  ) : null}
                    </fieldset>
                {/* 保存の失敗は確定の行の直上の 1 か所だけ（messaging.md §3.3.1。#585 / #1786）。 */}
                <SaveErrorBanner
                  message={formError || (activeView === "edit" ? actionError : "")}
                  attemptKey={submitAttempt}
                  testId="security-users-save-error"
                />
                {canSubmitUserForm ? (
                  // 確定はフォームの末尾の左（作成・保存 → 変更を破棄）。画面の下端に貼り付けない（#1786 / #2715）。
                  <FormActionBar
                    ariaLabel={t("security.users.formActionsLabel")}
                    testId="security-users-form-actions"
                    primaryActions={[
                      {
                        id: activeView === "edit" ? "save" : "create",
                        label: activeView === "edit" ? t("security.common.save") : t("security.common.submitCreate"),
                        icon: activeView === "edit" ? Save : Plus,
                        type: "submit",
                        loading: busy,
                        // 押したボタンは loading（aria-disabled）でフォーカスを保つ。ネイティブの disabled にしない（#355 / #835）。
                        disabled: operationBusy && !busy,
                        testId: "security-users-submit",
                      },
                    ]}
                    secondaryActions={[
                      {
                        id: "discard",
                        label: t("security.common.discardChanges"),
                        icon: RotateCcw,
                        disabled: !isDirty || operationBusy,
                        testId: "security-users-discard",
                        onClick: discardChanges,
                      },
                    ]}
                  />
                ) : null}
              </form>
            </SecurityManagementPanelShell>
          </>
        )}
      </PageBody>
    </>
  );
}

function UserStatusBadges({ user }: { user: SecurityUser }) {
  return (
    <div className="flex flex-wrap gap-1">
      <StatusBadge
        variant={user.status === "ACTIVE" ? "success" : "neutral"}
        label={userStatusLabel(user)}
      />
      {user.locked_until ? <StatusBadge variant="warning" label={t("security.users.locked")} /> : null}
    </div>
  );
}

function UserDetailPanel({
  user,
  assignedRoles,
  canManage,
  actions,
  resetError,
}: {
  user: SecurityUser;
  assignedRoles: AssignedRole[];
  canManage: boolean;
  actions: EntityAction[];
  resetError: string;
}) {
  const hasArchivedRole = assignedRoles.some((role) => role.archived);

  return (
    // 枠は一覧 + 詳細（ListDetailLayout）の詳細の列が持つ（カードの入れ子を 1 段減らす。#2282）。
    <section className="grid min-w-0 content-start gap-4" aria-labelledby="security-users-detail-heading" data-testid="security-users-detail">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id="security-users-detail-heading" className="flex min-w-0 items-center gap-2 text-base font-semibold text-fg">
              <UserRound size={20} aria-hidden="true" />
              <span className="min-w-0 break-words">{userDisplayName(user)}</span>
            </h2>
            <UserStatusBadges user={user} />
          </div>
        </div>
        {canManage ? (
          <ObjectActionBar
            actions={actions}
            ariaLabel={`${t("security.common.actions")}: ${userDisplayName(user)}`}
            testId="security-users-detail-actions"
          />
        ) : null}
      </div>
      <div data-testid="security-users-reset-password-error">
        <FormStatus tone="danger" message={resetError} />
      </div>
      <dl className="grid gap-3 md:grid-cols-2">
        <SecurityDetailField label={t("security.users.loginUserId")}>
          <code className="break-all font-mono text-xs">{user.login_user_id}</code>
        </SecurityDetailField>
        <SecurityDetailField label={t("security.common.status")}>
          <UserStatusBadges user={user} />
        </SecurityDetailField>
        <SecurityDetailField label={t("security.users.forceChange")}>
          {user.force_password_change ? t("security.common.yes") : t("security.common.no")}
        </SecurityDetailField>
        <SecurityDetailField label={t("security.common.version")}>
          {String(user.version)}
        </SecurityDetailField>
      </dl>

      <section className="grid gap-2" aria-label={t("security.users.roles")}>
        <h3 className="text-sm font-semibold text-fg">{t("security.users.roles")}</h3>
        {hasArchivedRole ? (
          <Banner severity="warning">{t("security.users.archivedRoleNotice")}</Banner>
        ) : null}
        {assignedRoles.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {assignedRoles.map((role) => (
              <StatusBadge
                icon={false}
                key={role.role_id}
                variant={role.archived ? "neutral" : "info"}
                label={assignedRoleLabel(role)}
              />
            ))}
          </div>
        ) : (
          <p className="rounded-md border border-dashed border-border p-3 text-sm text-fg-muted">{t("security.common.none")}</p>
        )}
      </section>
    </section>
  );
}
