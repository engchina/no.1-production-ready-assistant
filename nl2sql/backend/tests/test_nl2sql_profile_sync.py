from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from app.features.nl2sql.models import (
    AssetRefreshData,
    Nl2SqlEngine,
    Nl2SqlProfile,
    ProfileSelectAiConfig,
    ProfileSyncJobRequest,
    ProfileSyncJobStatus,
    SelectAiDbProfileMutationData,
)
from app.features.nl2sql.ontology_store import InMemoryOntologyStore
from app.features.nl2sql.oracle_adapter import (
    OracleNl2SqlAdapter,
    SelectAiCredentialMissingError,
)
from app.features.nl2sql.profile_sync import ProfileSyncService
from app.features.nl2sql.service import Nl2SqlService, SelectAiDbProfileListRefreshSync
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.settings import Settings


class _FakeProfileService:
    def __init__(self) -> None:
        self.profile = Nl2SqlProfile(id="profile-1", name="請求分析", etag="etag-1")
        self.oracle_calls = 0
        self.oracle_original_names: list[str] = []
        self.agent_calls = 0
        self.fail_oracle = False

    def get_profile(self, profile_id: str) -> Nl2SqlProfile:
        if profile_id != self.profile.id:
            raise ValueError("指定された業務プロファイルが見つかりません。")
        return self.profile.model_copy(deep=True)

    def upsert_profile_select_ai_profile(
        self,
        profile_id: str,
        request: object,
    ) -> SelectAiDbProfileMutationData:
        assert profile_id == self.profile.id
        self.oracle_calls += 1
        self.oracle_original_names.append(str(getattr(request, "original_name", "")))
        if self.fail_oracle:
            raise TimeoutError("Oracle round-trip timeout")
        return SelectAiDbProfileMutationData(
            executed=True,
            status="updated",
            profile_name="INVOICE_PROFILE",
        )

    def clear_profile_select_ai_previous_name(
        self,
        profile_id: str,
        *,
        expected_etag: str | None = None,
    ) -> Nl2SqlProfile:
        assert profile_id == self.profile.id
        assert expected_etag in {None, self.profile.etag}
        self.profile = self.profile.model_copy(
            update={
                "etag": f"{self.profile.etag}-cleared",
                "select_ai_config": self.profile.select_ai_config.model_copy(
                    update={"previous_profile_name": ""}
                ),
            }
        )
        return self.profile.model_copy(deep=True)

    def refresh_select_ai_agent_assets(
        self,
        profile_id: str,
        *,
        profile_already_synced: bool = False,
    ) -> AssetRefreshData:
        assert profile_id == self.profile.id
        assert profile_already_synced is True
        self.agent_calls += 1
        return AssetRefreshData(
            engine=Nl2SqlEngine.SELECT_AI_AGENT,
            refreshed=True,
            status="ready",
        )


class _ProfileSyncOracleAdapter:
    def __init__(self) -> None:
        self.active_profiles: set[str] = set()
        self.original_names: list[str] = []
        self.object_lists: dict[str, list[dict[str, str]]] = {}

    def upsert_select_ai_profile_low_level(
        self,
        *,
        profile_name: str,
        attributes: dict[str, object],
        description: str,
        original_name: str,
    ) -> dict[str, object]:
        del description
        self.original_names.append(original_name)
        if original_name:
            self.active_profiles.discard(original_name)
        self.active_profiles.discard(profile_name)
        self.active_profiles.add(profile_name)
        raw_object_list = attributes.get("object_list", [])
        object_list = raw_object_list if isinstance(raw_object_list, list) else []
        self.object_lists[profile_name] = [
            dict(item) for item in object_list if isinstance(item, dict)
        ]
        return {"runtime": "oracle"}

    def get_select_ai_profile_detail(self, *, profile_name: str) -> dict[str, object]:
        return {
            "name": profile_name,
            "object_list": self.object_lists.get(profile_name, []),
        }


class _OracleRuntimeProfileService(Nl2SqlService):
    def __init__(self, adapter: _ProfileSyncOracleAdapter) -> None:
        super().__init__(store=MemoryNl2SqlStore())
        self._oracle_adapter = adapter  # type: ignore[assignment]  # noqa: SLF001

    def _use_oracle_runtime(self) -> bool:
        return True

    def _submit_select_ai_db_profile_list_refresh_after_mutation(
        self,
        **_kwargs: object,
    ) -> SelectAiDbProfileListRefreshSync:
        return SelectAiDbProfileListRefreshSync()


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        nl2sql_ontology_worker_mode="external",
        nl2sql_profile_sync_job_timeout_seconds=300.0,
    )


def test_profile_sync_is_idempotent_and_agent_reuses_oracle_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    store = InMemoryOntologyStore()
    service = _FakeProfileService()
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
    request = ProfileSyncJobRequest(
        confirmation="ADMIN_EXECUTE",
        rebuild_agent_assets=True,
    )

    first = sync.start("profile-1", request, idempotency_key="same-request")
    duplicate = sync.start("profile-1", request, idempotency_key="same-request")
    assert duplicate.job_id == first.job_id

    completed = sync.run_persisted(first.job_id)
    assert completed.status == ProfileSyncJobStatus.SUCCEEDED
    assert service.oracle_calls == 1
    assert service.agent_calls == 1


def test_concurrent_profile_sync_submissions_share_one_persisted_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    store = InMemoryOntologyStore()
    service = _FakeProfileService()
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
    request = ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE")

    with ThreadPoolExecutor(max_workers=8) as executor:
        jobs = list(
            executor.map(
                lambda _index: sync.start(
                    "profile-1",
                    request,
                    idempotency_key="concurrent-request",
                ),
                range(16),
            )
        )

    assert len({job.job_id for job in jobs}) == 1
    assert len(store.list_documents("jobs", {"profile_id": "profile-1"})) == 1


def test_profile_sync_failure_is_persisted_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    store = InMemoryOntologyStore()
    service = _FakeProfileService()
    service.fail_oracle = True
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
    request = ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE")

    started = sync.start("profile-1", request, idempotency_key="will-fail")
    failed = sync.run_persisted(started.job_id)
    assert failed.status == ProfileSyncJobStatus.FAILED
    assert "Oracle round-trip timeout" in failed.error_message_ja

    service.fail_oracle = False
    retried = sync.retry(failed.job_id)
    completed = sync.run_persisted(retried.job_id)
    assert completed.status == ProfileSyncJobStatus.SUCCEEDED
    assert completed.retry_of_job_id == failed.job_id
    assert service.oracle_calls == 2


def test_profile_sync_passes_original_name_after_profile_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    store = InMemoryOntologyStore()
    service = _FakeProfileService()
    service.profile = service.profile.model_copy(
        update={
            "name": "INVOICE_PROFILE_V2",
            "select_ai_config": service.profile.select_ai_config.model_copy(
                update={
                    "profile_name": "INVOICE_PROFILE_V2",
                    "previous_profile_name": "INVOICE_PROFILE",
                }
            ),
        }
    )
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]

    started = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
        idempotency_key="renamed-profile",
    )
    completed = sync.run_persisted(started.job_id)

    assert started.original_name == "INVOICE_PROFILE"
    assert completed.status == ProfileSyncJobStatus.SUCCEEDED
    assert service.oracle_original_names == ["INVOICE_PROFILE"]


def test_profile_sync_clears_previous_name_after_each_successful_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    adapter = _ProfileSyncOracleAdapter()
    adapter.active_profiles.add("SALES_PROFILE")
    service = _OracleRuntimeProfileService(adapter)
    service.create_profile(
        Nl2SqlProfile(
            id="profile-1",
            name="SALES_PROFILE",
            allowed_tables=["APP.INVOICES"],
            select_ai_config=ProfileSelectAiConfig(profile_name="SALES_PROFILE"),
        )
    )
    store = InMemoryOntologyStore()
    sync = ProfileSyncService(service=service, store_provider=lambda: store)

    service.update_profile(
        "profile-1",
        lambda profile: profile.model_copy(
            update={
                "name": "SALES_V2",
                "select_ai_config": profile.select_ai_config.model_copy(
                    update={"profile_name": "SALES_V2"}
                ),
            }
        ),
    )
    first = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
        idempotency_key="sales-v2",
    )
    first_completed = sync.run_persisted(first.job_id)

    assert first_completed.status == ProfileSyncJobStatus.SUCCEEDED
    assert service.get_profile("profile-1").select_ai_config.previous_profile_name == ""

    service.update_profile(
        "profile-1",
        lambda profile: profile.model_copy(
            update={
                "name": "SALES_V3",
                "select_ai_config": profile.select_ai_config.model_copy(
                    update={"profile_name": "SALES_V3"}
                ),
            }
        ),
    )
    assert service.get_profile("profile-1").select_ai_config.previous_profile_name == "SALES_V2"
    second = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
        idempotency_key="sales-v3",
    )
    second_completed = sync.run_persisted(second.job_id)

    assert second_completed.status == ProfileSyncJobStatus.SUCCEEDED
    assert adapter.original_names == ["SALES_PROFILE", "SALES_V2"]
    assert adapter.active_profiles == {"SALES_V3"}


def test_profile_sync_scope_mismatch_keeps_profile_list_refresh_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反映が範囲の不一致で失敗しても、DB Profile 一覧の同期の要否を job の結果に残す（#2034）。"""

    class ScopeMismatchService(_FakeProfileService):
        def upsert_profile_select_ai_profile(
            self,
            profile_id: str,
            _request: object,
        ) -> SelectAiDbProfileMutationData:
            assert profile_id == self.profile.id
            self.oracle_calls += 1
            return SelectAiDbProfileMutationData(
                executed=False,
                status="scope_mismatch",
                profile_name="INVOICE_PROFILE",
                warnings=["DBMS_CLOUD_AI Profile の対象の範囲が一致しません。"],
                profile_list_refresh_required=True,
                profile_list_refresh_reason_code="profile_list_refresh_target_unresolved",
            )

    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    store = InMemoryOntologyStore()
    service = ScopeMismatchService()
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
    started = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
        idempotency_key="scope-mismatch",
    )

    failed = sync.run_persisted(started.job_id)

    assert failed.status == ProfileSyncJobStatus.FAILED
    assert failed.oracle_result is not None
    assert failed.oracle_result.status == "scope_mismatch"
    assert failed.oracle_result.profile_list_refresh_required is True
    assert "DB Profile 一覧" not in failed.error_message_ja


def test_profile_sync_credential_missing_uses_recoverable_code_without_oracle_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingCredentialService(_FakeProfileService):
        def upsert_profile_select_ai_profile(
            self,
            profile_id: str,
            _request: object,
        ) -> SelectAiDbProfileMutationData:
            assert profile_id == self.profile.id
            self.oracle_calls += 1
            error = SelectAiCredentialMissingError("NL2SQL_OCI_CRED", "ADMIN")
            error.__cause__ = RuntimeError("ORA-06512: at line 2291 private stack")
            raise error

    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    store = InMemoryOntologyStore()
    service = MissingCredentialService()
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
    started = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
        idempotency_key="missing-credential",
    )

    failed = sync.run_persisted(started.job_id)

    assert failed.status == ProfileSyncJobStatus.FAILED
    assert failed.error_code == "SELECT_AI_CREDENTIAL_MISSING"
    assert "NL2SQL_OCI_CRED" in failed.error_message_ja
    # Credential は運用設定の専用の画面で作る（#658）。作る場所を案内する（#1683）。
    assert "運用設定の「Select AI Credential」で作成してから" in failed.error_message_ja
    assert "ORA-" not in failed.error_message_ja
    retried = sync.retry(failed.job_id)
    assert retried.retry_of_job_id == failed.job_id


def test_profile_sync_agent_rebuild_failure_keeps_oracle_result_and_failed_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DBMS_CLOUD_AI Profile の反映後に Agent の再構築だけが失敗したら、一部の成功として残す。"""

    class AgentFailureService(_FakeProfileService):
        def refresh_select_ai_agent_assets(
            self,
            profile_id: str,
            *,
            profile_already_synced: bool = False,
        ) -> AssetRefreshData:
            assert profile_already_synced is True
            self.agent_calls += 1
            return AssetRefreshData(
                engine=Nl2SqlEngine.SELECT_AI_AGENT,
                refreshed=False,
                status="error",
                warning="ORA-20051: Agent team の作成に失敗 at line 12 private stack",
            )

    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    store = InMemoryOntologyStore()
    service = AgentFailureService()
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
    started = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE", rebuild_agent_assets=True),
        idempotency_key="agent-failure",
    )

    failed = sync.run_persisted(started.job_id)

    assert failed.status == ProfileSyncJobStatus.FAILED
    assert failed.failed_phase == "rebuilding_agent_assets"
    assert failed.oracle_result is not None and failed.oracle_result.executed is True
    assert failed.error_code == "AGENT_ASSETS_REBUILD_FAILED"
    assert "Select AI Agent アセットを再構築できませんでした" in failed.error_message_ja
    assert "Oracle Profile の反映に失敗" not in failed.error_message_ja
    assert "ORA-" not in failed.error_message_ja
    assert service.oracle_calls == 1
    assert service.agent_calls == 1


def test_profile_sync_oracle_failure_records_failed_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    store = InMemoryOntologyStore()
    service = _FakeProfileService()
    service.fail_oracle = True
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
    started = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE", rebuild_agent_assets=True),
        idempotency_key="oracle-failure-phase",
    )

    failed = sync.run_persisted(started.job_id)

    assert failed.failed_phase == "syncing_oracle_profile"
    assert failed.oracle_result is None
    assert failed.error_code == "PROFILE_SYNC_FAILED"
    assert service.agent_calls == 0


def test_profile_sync_can_be_cancelled_before_worker_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    store = InMemoryOntologyStore()
    service = _FakeProfileService()
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
    started = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
        idempotency_key="cancel-me",
    )

    assert sync.cancel_for_profile("profile-1") == 1
    cancelled = sync.run_persisted(started.job_id)
    assert cancelled.status == ProfileSyncJobStatus.CANCELLED
    assert service.oracle_calls == 0


def test_oracle_connection_applies_round_trip_timeout_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        call_timeout = 0
        closed = False
        rollbacks = 0

        def close(self) -> None:
            self.closed = True

        def rollback(self) -> None:
            self.rollbacks += 1

        def cursor(self) -> Any:
            # 接続ごとの初期化（ALTER SESSION）を受け流す。
            return nullcontext(SimpleNamespace(execute=lambda _statement: None))

    connection = Connection()

    class Driver:
        @staticmethod
        def connect(**_kwargs: object) -> Connection:
            return connection

    adapter = OracleNl2SqlAdapter(
        Settings(
            oracle_user="APP",
            oracle_password="password",
            oracle_dsn="localhost/FREEPDB1",
            # CI には Wallet が無いため wallet 不要モードで接続 kwargs を組み立てる。
            oracle_connection_security="walletless_tls",
            nl2sql_oracle_call_timeout_seconds=12.5,
        )
    )
    monkeypatch.setattr(adapter, "_load_oracledb", lambda: Driver())
    monkeypatch.setattr(adapter, "_init_client", lambda _driver: None)

    with pytest.raises(TimeoutError, match="round-trip"), adapter.connection() as opened:
        assert opened is connection
        assert connection.call_timeout == 12_500
        raise TimeoutError("round-trip")

    assert connection.closed is True
    assert connection.rollbacks == 1


def test_drop_profile_compatibility_signatures_stop_after_first_success() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _sql: str, _params: object) -> None:
            self.calls += 1

    cursor = Cursor()
    adapter = OracleNl2SqlAdapter(Settings())

    adapter._drop_cloud_ai_profile_best_effort(cursor, "INVOICE_PROFILE")  # noqa: SLF001

    assert cursor.calls == 1


def test_select_ai_credential_preflight_happens_before_profile_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Cursor:
        result: tuple[object, ...] = ()

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object | None = None) -> None:
            statements.append(sql)
            if "CURRENT_SCHEMA" in sql:
                self.result = ("ADMIN",)
            elif "USER_CREDENTIALS" in sql:
                self.result = (0,)
            else:
                raise AssertionError(
                    "Credential preflight 後に Profile mutation が実行されました。"
                )

        def fetchone(self) -> tuple[object, ...]:
            return self.result

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    adapter = OracleNl2SqlAdapter(Settings())

    @contextmanager  # type: ignore[arg-type]
    def fake_connection() -> object:
        yield Connection()

    monkeypatch.setattr(adapter, "connection", fake_connection)

    with pytest.raises(SelectAiCredentialMissingError) as exc_info:
        adapter.upsert_select_ai_profile_low_level(
            profile_name="INVOICE_PROFILE",
            original_name="OLD_PROFILE",
            attributes={"provider": "oci", "credential_name": "NL2SQL_OCI_CRED"},
        )

    assert exc_info.value.code == "SELECT_AI_CREDENTIAL_MISSING"
    assert any("USER_CREDENTIALS" in sql for sql in statements)
    assert all("DROP_PROFILE" not in sql for sql in statements)
    assert all("CREATE_PROFILE" not in sql for sql in statements)


@pytest.mark.parametrize("status", ["queued", "running"])
def test_profile_sync_old_job_expires_on_read_without_oracle_replay(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    from app.features.nl2sql.models import ProfileSyncJobData

    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    store = InMemoryOntologyStore()
    service = _FakeProfileService()
    job = ProfileSyncJobData(
        job_id="old-sync",
        profile_id="profile-1",
        status=status,
        created_at="2020-01-01T00:00:00+00:00",
    )
    store.save_job(
        {
            "job_id": job.job_id,
            "job_type": "profile_sync",
            "profile_id": job.profile_id,
            "status": status,
            "payload": job.model_dump(mode="json"),
        }
    )
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
    assert sync.peek(job.job_id).status.value == status  # type: ignore[union-attr]
    expired = sync.get(job.job_id)
    assert expired and expired.status == ProfileSyncJobStatus.FAILED
    assert expired.error_code == "PROFILE_SYNC_TIMEOUT"
    assert "状態を確認" in expired.error_message_ja
    assert sync.run_persisted(job.job_id).status == ProfileSyncJobStatus.FAILED
    assert service.oracle_calls == 0


@pytest.mark.parametrize("ending", ["shutdown", "timeout", "cancel"])
def test_profile_sync_late_oracle_return_does_not_update_state_or_start_agent(
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    import threading

    configured = _settings()
    configured.nl2sql_ontology_worker_mode = "inprocess"
    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", lambda: configured)
    started, release = threading.Event(), threading.Event()
    threads: list[threading.Thread] = []
    original_start = threading.Thread.start

    def start(thread: threading.Thread) -> None:
        threads.append(thread)
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start)

    class BlockingService(_FakeProfileService):
        def upsert_profile_select_ai_profile(
            self, profile_id: str, request: object
        ) -> SelectAiDbProfileMutationData:
            started.set()
            assert release.wait(5)
            return super().upsert_profile_select_ai_profile(profile_id, request)

    store = InMemoryOntologyStore()
    service = BlockingService()
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
    queued = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE", rebuild_agent_assets=True),
        idempotency_key=ending,
    )
    try:
        assert started.wait(5)
        observer = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
        observer.shutdown()
        assert observer.get(queued.job_id).status == ProfileSyncJobStatus.RUNNING  # type: ignore[union-attr]
        if ending == "shutdown":
            sync.shutdown()
        elif ending == "timeout":
            monkeypatch.setattr(ProfileSyncService, "_expired", staticmethod(lambda job: True))
        else:
            observer.cancel_for_profile("profile-1")
        terminal = observer.get(queued.job_id)
        assert terminal and terminal.status in {
            ProfileSyncJobStatus.FAILED,
            ProfileSyncJobStatus.CANCELLED,
        }
        if ending != "cancel":
            with pytest.raises(ValueError, match="前回の Oracle 呼び出し"):
                sync.retry(queued.job_id)
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    assert sync.get(queued.job_id) == terminal
    assert service.oracle_calls == 1 and service.agent_calls == 0


def test_profile_sync_concurrent_claim_only_mutates_oracle_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    store = InMemoryOntologyStore()
    service = _FakeProfileService()
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
    job = sync.start(
        "profile-1", ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"), idempotency_key="race"
    )
    barrier = threading.Barrier(2)
    original = sync._save

    def save(value: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("claim"):
            barrier.wait(timeout=5)
        return original(value, **kwargs)

    monkeypatch.setattr(sync, "_save", save)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(sync.run_persisted, [job.job_id, job.job_id]))
    assert service.oracle_calls == 1
    assert sync.run_persisted(job.job_id).status == ProfileSyncJobStatus.SUCCEEDED
    assert service.oracle_calls == 1


# --- 保存（Oracle 反映）の事前条件: Select AI の Credential（#1683） ---------------------------


class _CredentialStatusAdapter:
    """`get_select_ai_credential_status` だけを持つ adapter（USER_CREDENTIALS の結果）。"""

    def __init__(self, *, exists: bool | None) -> None:
        self.exists = exists
        self.calls: list[str] = []

    def get_select_ai_credential_status(self, credential_name: str) -> tuple[str, bool]:
        self.calls.append(credential_name)
        if self.exists is None:
            raise RuntimeError("DPY-6005: cannot connect to database")
        return "ADMIN", self.exists


def _oracle_settings() -> SimpleNamespace:
    return SimpleNamespace(
        nl2sql_ontology_worker_mode="external",
        nl2sql_profile_sync_job_timeout_seconds=300.0,
        nl2sql_runtime_mode="oracle",
        nl2sql_select_ai_credential_name="nl2sql_oci_cred",
    )


def _credential_sync(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exists: bool | None,
    settings: Any = _oracle_settings,
) -> tuple[ProfileSyncService, _FakeProfileService, _CredentialStatusAdapter, Any]:
    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", settings)
    store = InMemoryOntologyStore()
    service = _FakeProfileService()
    adapter = _CredentialStatusAdapter(exists=exists)
    service._oracle_adapter = adapter  # type: ignore[attr-defined]  # noqa: SLF001
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
    return sync, service, adapter, store


def test_profile_sync_start_without_credential_stops_before_creating_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync, service, adapter, store = _credential_sync(monkeypatch, exists=False)

    readiness = sync.readiness()
    assert readiness.credential_required is True
    assert readiness.credential_name == "NL2SQL_OCI_CRED"
    assert readiness.credential_exists is False

    with pytest.raises(SelectAiCredentialMissingError) as exc_info:
        sync.start(
            "profile-1",
            ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
            idempotency_key="no-credential",
        )

    assert exc_info.value.credential_name == "NL2SQL_OCI_CRED"
    assert exc_info.value.schema_name == "ADMIN"
    assert adapter.calls == ["NL2SQL_OCI_CRED", "NL2SQL_OCI_CRED"]
    # job も Idempotency-Key の予約も作らず、Oracle の反映（CREATE_PROFILE）にも進まない。
    assert store.list_documents("jobs", {"profile_id": "profile-1"}) == []
    assert store.get_idempotency("profile_sync", "no-credential") is None
    assert service.oracle_calls == 0


def test_profile_sync_start_with_credential_queues_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync, service, adapter, _store = _credential_sync(monkeypatch, exists=True)

    started = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
        idempotency_key="with-credential",
    )
    assert started.status == ProfileSyncJobStatus.QUEUED
    assert adapter.calls == ["NL2SQL_OCI_CRED"]
    # 同じ要求の再送は受付済みの job を返し、事前条件を確かめ直さない。
    again = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
        idempotency_key="with-credential",
    )
    assert again.job_id == started.job_id
    assert adapter.calls == ["NL2SQL_OCI_CRED"]
    completed = sync.run_persisted(started.job_id)
    assert completed.status == ProfileSyncJobStatus.SUCCEEDED
    assert service.oracle_calls == 1


@pytest.mark.parametrize(
    ("runtime_mode", "credential_name"),
    [("deterministic", "NL2SQL_OCI_CRED"), ("oracle", "")],
    ids=["deterministic-runtime", "credential-name-unset"],
)
def test_profile_sync_readiness_skips_credential_check_when_not_referenced(
    monkeypatch: pytest.MonkeyPatch,
    runtime_mode: str,
    credential_name: str,
) -> None:
    def settings() -> SimpleNamespace:
        return SimpleNamespace(
            nl2sql_ontology_worker_mode="external",
            nl2sql_profile_sync_job_timeout_seconds=300.0,
            nl2sql_runtime_mode=runtime_mode,
            nl2sql_select_ai_credential_name=credential_name,
        )

    sync, _service, adapter, _store = _credential_sync(monkeypatch, exists=False, settings=settings)

    readiness = sync.readiness()
    assert readiness.credential_required is False
    assert readiness.credential_exists is None
    started = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
        idempotency_key=f"skip-{runtime_mode}",
    )
    assert started.status == ProfileSyncJobStatus.QUEUED
    assert adapter.calls == []


def test_profile_sync_credential_status_failure_does_not_block_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync, _service, adapter, _store = _credential_sync(monkeypatch, exists=None)

    readiness = sync.readiness()
    assert readiness.credential_required is True
    assert readiness.credential_exists is None
    # 確かめられないときは止めない（反映の手順が同じ失敗を返す）。
    started = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
        idempotency_key="status-unavailable",
    )
    assert started.status == ProfileSyncJobStatus.QUEUED
    assert adapter.calls


def test_profile_sync_retry_without_credential_stops_before_creating_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync, service, adapter, store = _credential_sync(monkeypatch, exists=True)
    service.fail_oracle = True
    started = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
        idempotency_key="retry-credential",
    )
    failed = sync.run_persisted(started.job_id)
    assert failed.status == ProfileSyncJobStatus.FAILED

    adapter.exists = False
    with pytest.raises(SelectAiCredentialMissingError):
        sync.retry(failed.job_id)
    jobs = store.list_documents("jobs", {"profile_id": "profile-1"})
    assert [document["job_id"] for document in jobs] == [failed.job_id]


def test_profile_sync_maps_credential_ora_from_create_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事前の確認の後に Credential が消えたとき、CREATE_PROFILE の ORA-20004 も同じ案内にする。"""

    class CreateProfileCredentialOraService(_FakeProfileService):
        def upsert_profile_select_ai_profile(
            self,
            profile_id: str,
            _request: object,
        ) -> SelectAiDbProfileMutationData:
            self.oracle_calls += 1
            # service は OracleAdapterError を warnings に入れた error の結果を返す（実 ADB の文）。
            return SelectAiDbProfileMutationData(
                runtime="oracle",
                executed=False,
                status="error",
                profile_name="INVOICE_PROFILE",
                warnings=[
                    "Oracle PL/SQL 実行に失敗しました: ORA-20004: Credential "
                    '"ADMIN"."NL2SQL_OCI_CRED" does not exist\nORA-06512: at line 1'
                ],
            )

    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    store = InMemoryOntologyStore()
    service = CreateProfileCredentialOraService()
    sync = ProfileSyncService(service=service, store_provider=lambda: store)  # type: ignore[arg-type]
    started = sync.start(
        "profile-1",
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
        idempotency_key="credential-ora",
    )

    failed = sync.run_persisted(started.job_id)

    assert failed.status == ProfileSyncJobStatus.FAILED
    assert failed.error_code == "SELECT_AI_CREDENTIAL_MISSING"
    assert "Select AI Credential" in failed.error_message_ja
    assert "ORA-" not in failed.error_message_ja


def test_profile_sync_router_returns_409_without_credential_and_202_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.features.nl2sql import profile_sync as profile_sync_module
    from app.features.nl2sql import router as nl2sql_router

    sync, _service, adapter, _store = _credential_sync(monkeypatch, exists=False)
    monkeypatch.setattr(profile_sync_module, "profile_sync_service", sync)
    monkeypatch.setattr(nl2sql_router, "_assert_profile_access", lambda *_a, **_k: None)
    # 状態の API は共通の確認（select_ai_readiness）で、service の adapter を読む（#1688）。
    monkeypatch.setattr("app.features.nl2sql.select_ai_readiness.get_settings", _oracle_settings)
    from app.features.nl2sql.service import nl2sql_service

    monkeypatch.setattr(nl2sql_service, "_oracle_adapter", adapter)
    app = FastAPI()
    app.include_router(nl2sql_router.router, prefix="/api")
    app.dependency_overrides[nl2sql_router._require_persistence] = lambda: None  # noqa: SLF001
    body = {"confirmation": "ADMIN_EXECUTE", "rebuild_agent_assets": False}

    with TestClient(app) as client:
        readiness = client.get("/api/nl2sql/select-ai/credential-readiness")
        assert readiness.status_code == 200
        assert readiness.json()["data"] == {
            "credential_required": True,
            "credential_name": "NL2SQL_OCI_CRED",
            "credential_exists": False,
        }

        missing = client.post(
            "/api/nl2sql/profiles/profile-1/oracle-sync-jobs",
            json=body,
            headers={"Idempotency-Key": "router-missing"},
        )
        assert missing.status_code == 409
        assert "SELECT_AI_CREDENTIAL_MISSING" in missing.text
        assert "運用設定の「Select AI Credential」で作成してから" in missing.text
        assert "ORA-" not in missing.text

        adapter.exists = True
        created = client.post(
            "/api/nl2sql/profiles/profile-1/oracle-sync-jobs",
            json=body,
            headers={"Idempotency-Key": "router-ready"},
        )
        assert created.status_code == 202
        assert created.json()["data"]["status"] == "queued"
