"""共通の単一設定・既定の無効・cache・失敗時の無効を検証する。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pr_system_settings.trace_recording import (
    InMemoryTraceRecordingStore,
    TraceRecordingProvider,
    build_trace_recording_router,
)


def test_shared_switch_default_cache_and_saved_value():
    clock = [0.0]
    store = InMemoryTraceRecordingStore()
    first = TraceRecordingProvider(store, clock=lambda: clock[0])
    second = TraceRecordingProvider(store, clock=lambda: clock[0])
    assert not first.enabled() and not second.enabled()
    first.save(True)
    assert first.enabled() is True and second.enabled() is False
    clock[0] = 5.1
    assert second.enabled() is True
    second.save(False)
    clock[0] += 5.1
    assert first.enabled() is False


def test_failure_is_not_a_last_known_on_value():
    class Broken(InMemoryTraceRecordingStore):
        broken = False

        def load(self):
            if self.broken:
                raise RuntimeError("private")
            return super().load()

    clock = [0.0]
    store = Broken()
    store.save(True)
    provider = TraceRecordingProvider(store, clock=lambda: clock[0])
    assert provider.enabled() is True
    store.broken = True
    clock[0] = 6.0
    assert provider.enabled() is False


def test_api_persists_strict_boolean_and_keeps_metadata():
    provider = TraceRecordingProvider(InMemoryTraceRecordingStore())
    app = FastAPI()
    app.include_router(build_trace_recording_router(provider=lambda: provider), prefix="/settings")
    client = TestClient(app)
    assert client.get("/settings/trace-recording").json()["data"]["enabled"] is False
    assert (
        client.put("/settings/trace-recording", json={"enabled": True}).json()["data"]["enabled"]
        is True
    )
    assert client.put("/settings/trace-recording", json={"enabled": "false"}).status_code == 422
    assert (
        client.put(
            "/settings/trace-recording", json={"enabled": False, "arbitrary": True}
        ).status_code
        == 422
    )


def test_policy_cache_does_not_read_the_database_for_each_call():
    class Counting(InMemoryTraceRecordingStore):
        reads = 0

        def load(self):
            self.reads += 1
            return super().load()

    clock = [0.0]
    store = Counting()
    provider = TraceRecordingProvider(store, clock=lambda: clock[0])
    for _ in range(1000):
        assert provider.enabled() is False
    assert store.reads == 1
    clock[0] = 6.0
    provider.enabled()
    assert store.reads == 2


def test_write_dependency_blocks_an_unauthorized_change():
    from fastapi import Depends, HTTPException

    def deny():
        raise HTTPException(403)

    provider = TraceRecordingProvider(InMemoryTraceRecordingStore())
    app = FastAPI()
    app.include_router(
        build_trace_recording_router(provider=lambda: provider, write_dependencies=[Depends(deny)])
    )
    assert TestClient(app).put("/trace-recording", json={"enabled": True}).status_code == 403
    assert provider.enabled() is False
