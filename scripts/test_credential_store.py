"""Credential semantics and isolated Windows-vault roundtrip checks.

Only fake secrets in a fresh ORCA/credential-test/<uuid> namespace are touched.
Existing ORCA or other Windows credentials are never enumerated or read.
"""
import json
import multiprocessing
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
os.environ.setdefault("PDFMT_DATA_DIR", str(ROOT / "test-data" / "credential-store"))
os.environ.setdefault("PDFMT_CACHE_DIR", str(Path(os.environ["PDFMT_DATA_DIR"]) / "cache" / "babeldoc"))


class MemoryBackend:
    def __init__(self):
        self.data = {}
        self.operations = []

    def read(self, target):
        self.operations.append(("read", target))
        return self.data.get(target)

    def write(self, target, blob):
        self.operations.append(("write", target))
        self.data[target] = blob

    def delete(self, target):
        self.operations.append(("delete", target))
        self.data.pop(target, None)


def semantics():
    from credential_store import CredentialStore
    namespace = "ORCA/credential-test/memory-" + uuid.uuid4().hex
    backend = MemoryBackend()
    store = CredentialStore(namespace)
    store._backend = backend
    settings = {"openai_compatible_api_key": "fake-alpha-only", "openai_compatible_base_url": "https://example.invalid/v1", "openai_compatible_model": "model-a", "openai_api_key": "foreign-provider-key", "unknown_api_key": "not-a-supported-field"}
    assert store.load("custom", settings) == {"ok": True, "remembered": False, "secrets": {}}
    assert not backend.data, "Reading must never persist keys"
    saved = store.save("custom", settings)
    assert saved == {"ok": True, "remembered": True}
    assert "fake-alpha-only" not in json.dumps(saved)
    actual = json.loads(next(iter(backend.data.values())))
    assert actual["secrets"] == {"openai_compatible_api_key": "fake-alpha-only"}
    assert "model-a" not in json.dumps(actual) and "example.invalid" not in json.dumps(actual)
    assert store.load("OPENAICOMPATIBLE", {**settings, "openai_compatible_model": "model-b"})["secrets"] == actual["secrets"]
    same = store.load("custom", {**settings, "openai_compatible_base_url": "https://EXAMPLE.INVALID/v1/chat/completions/"})
    assert same["secrets"] == actual["secrets"], same
    mismatch = store.load("custom", {**settings, "openai_compatible_base_url": "https://another.invalid/v1"})
    assert mismatch == {"ok": True, "remembered": True, "secrets": {}, "endpoint_mismatch": True}
    assert store.load("custom", {**settings, "openai_compatible_base_url": "https://example.invalid/other"})["endpoint_mismatch"]
    assert store.load("custom", {**settings, "openai_compatible_base_url": "https://example.invalid/v1?tenant=2"})["endpoint_mismatch"]
    userinfo_settings = {**settings, "openai_compatible_base_url": "https://User:Password@example.invalid/v1"}
    assert store.save("custom", userinfo_settings)["ok"]
    assert store.load("custom", {**userinfo_settings, "openai_compatible_base_url": "https://User:password@example.invalid/v1"})["endpoint_mismatch"]
    assert store.load("custom", {**userinfo_settings, "openai_compatible_base_url": "https://user:Password@example.invalid/v1"})["endpoint_mismatch"]
    assert store.save("custom", settings)["ok"]
    assert store.save("DeepL", {"deepl_auth_key": "fake-deepl-only"})["ok"]
    assert store.load("deepl", {})["secrets"] == {"deepl_auth_key": "fake-deepl-only"}
    assert store.save("tencent", {"tencentcloud_secret_id": "fake-id", "tencentcloud_secret_key": "fake-secret"})["ok"]
    assert len(store.load("tencent", {})["secrets"]) == 2
    assert store.save("openai", {"openai_api_key": "fake-openai-only"})["ok"]
    openai_default = store.load("openai", {"openai_base_url": os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"})
    assert openai_default["remembered"] and openai_default["secrets"]
    assert not store.save("bing", {})["ok"]
    previous = dict(backend.data)
    for invalid in ({}, {**settings, "openai_compatible_api_key": " "}, {**settings, "openai_compatible_api_key": "x" * 3000}, {**settings, "openai_compatible_api_key": {"secret": "no"}}):
        result = store.save("custom", invalid)
        assert not result["ok"] and "fake-alpha-only" not in json.dumps(result)
        assert backend.data == previous, "Invalid save replaced an existing credential"
    count = len(backend.operations)
    assert not store.load("Microsoft/unknown", {})["ok"]
    assert not store.save("unknown", {})["ok"]
    assert not store.forget("unknown")["ok"]
    assert len(backend.operations) == count
    assert store.forget("custom")["ok"]
    assert not store.load("custom", settings)["remembered"]
    assert store.load("deepl", {})["remembered"], "Forgetting one provider deleted another"
    assert store.forget("custom")["ok"]
    backend.data[namespace + "/deepl"] = b"invalid record"
    corrupt = store.load("deepl", {})
    assert not corrupt["ok"] and corrupt["remembered"] and not corrupt["secrets"]
    backend.data[namespace + "/deepl"] = json.dumps({"version": 1, "engine": "deepl", "binding": "f" * 64, "secrets": {"unknown_api_key": "foreign"}}).encode()
    assert not store.load("deepl", {})["ok"]

    class FailingBackend(MemoryBackend):
        def write(self, *args):
            raise OSError("failure includes fake-alpha-only")
    failing = CredentialStore(namespace)
    failing._backend = FailingBackend()
    failed = failing.save("custom", settings)
    assert not failed["ok"] and "fake-alpha-only" not in json.dumps(failed)
    assert not failing._backend.data
    print("PASS: explicit saves, secret whitelist, per-provider isolation, endpoint binding, limits, failures, forget and corruption handling")


def _fresh_process_read(namespace, engine, settings, expected, send):
    from credential_store import CredentialStore
    try:
        result = CredentialStore(namespace).load(engine, settings)
        send.send({"ok": result["ok"], "remembered": result["remembered"], "matches": result.get("secrets") == expected})
    finally:
        send.close()


def windows_roundtrip():
    from credential_store import CredentialStore
    if os.name != "nt":
        return {"ok": False, "skipped": "Windows required"}
    namespace = "ORCA/credential-test/" + uuid.uuid4().hex
    store = CredentialStore(namespace)
    source = {"openai_compatible_api_key": "fake-test-secret-A-中文", "openai_compatible_base_url": "https://example.invalid/v1"}
    touched = ("openaicompatible", "deepl")
    try:
        assert store.load("custom", source)["remembered"] is False
        saved = store.save("custom", source)
        assert saved["ok"], saved
        assert store.save("deepl", {"deepl_auth_key": "fake-deepl-test"})["ok"]
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=_fresh_process_read, args=(namespace, "custom", source, {"openai_compatible_api_key": source["openai_compatible_api_key"]}, send))
        try:
            process.start()
            send.close()
            assert receive.poll(30), "Fresh credential reader timed out"
            result = receive.recv()
            assert result == {"ok": True, "remembered": True, "matches": True}, result
            process.join(5)
            assert process.exitcode == 0
        finally:
            send.close()
            receive.close()
            if process.is_alive():
                process.terminate()
                process.join(3)
            process.close()
        changed = {**source, "openai_compatible_api_key": "fake-test-secret-B"}
        assert store.save("custom", changed)["ok"]
        assert CredentialStore(namespace).load("custom", changed)["secrets"] == {"openai_compatible_api_key": "fake-test-secret-B"}
        assert CredentialStore(namespace).load("custom", {**changed, "openai_compatible_base_url": "https://other.invalid/v1"})["secrets"] == {}
        assert store.forget("custom")["ok"]
        assert CredentialStore(namespace).load("custom", changed)["remembered"] is False
        assert CredentialStore(namespace).load("deepl", {})["secrets"] == {"deepl_auth_key": "fake-deepl-test"}
        report = {"ok": True, "native_windows_vault": True, "fresh_process_read": True, "replace": True, "forget": True, "endpoint_binding": True}
    finally:
        for engine in touched:
            result = store.forget(engine)
            if not result["ok"]:
                raise RuntimeError("Test credential cleanup failed for " + namespace + "/" + engine)
    print("PASS: Windows credential write/read in fresh process, replace, endpoint mismatch and scoped deletion; test entries removed")
    return report


if __name__ == "__main__":
    multiprocessing.freeze_support()
    semantics()
    if "--memory-only" not in sys.argv:
        windows_roundtrip()
