"""ORCA v0.2 contract checks. No translation services are contacted."""
import asyncio
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
os.environ.setdefault("PDFMT_DATA_DIR", str(ROOT / "test-data/v02-backend"))
os.environ.setdefault("PDFMT_CACHE_DIR", str(ROOT / "test-data/v02-backend/cache"))

from engine_registry import get_catalog, build_engine, safe_settings, SecretFilter, redact, secret_values
from preferences import LocalStore
from desktop_api import DesktopApi


def main():
    catalog = get_catalog()
    ids = {item["id"] for item in catalog}
    assert len(ids) == 23
    assert {"bing", "indextranslate", "openai", "openaicompatible", "deepl", "deepseek", "ollama", "tencentmechinetranslation"} <= ids
    assert not {"cli", "clitranslator", "claudecode"} & ids
    for item in catalog:
        assert len({field["name"] for field in item["fields"]}) == len(item["fields"])
        assert all(field["default"] is None for field in item["fields"] if field["type"] == "password")
    index = next(item for item in catalog if item["id"] == "indextranslate")
    assert not any(field["type"] == "password" for field in index["fields"])
    assert build_engine({"engine": "indextranslate"}).translate_engine_type == "IndexTranslate"
    custom = {"engine": "OpenAICompatible", "engine_settings": {"openai_compatible_api_key": "not-a-real-secret-123", "openai_compatible_model": "my-private-model", "openai_compatible_base_url": "http://127.0.0.1:9999/v1", "openai_compatible_send_temperature": True, "openai_compatible_temperature": "0.5"}}
    engine = build_engine(custom)
    transformed = engine.transform()
    assert transformed.openai_model == "my-private-model"
    assert transformed.openai_base_url == "http://127.0.0.1:9999/v1"
    assert transformed.openai_send_temprature is True
    assert transformed.openai_temperature == "0.5"
    message = redact("problem not-a-real-secret-123", secret_values(custom))
    assert "not-a-real-secret" not in message
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(SecretFilter(secret_values(custom)))
    logger = logging.getLogger("v02-test-secret")
    logger.addHandler(handler)
    logger.error("provider failure: %s", "not-a-real-secret-123")
    logger.removeHandler(handler)
    assert "not-a-real-secret" not in buf.getvalue()

    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory)
        store = LocalStore(data)
        store.update({"theme": "dark", "engine": "openaicompatible", "engine_settings": {"openaicompatible": custom["engine_settings"], "deepl": {"deepl_auth_key": "deepl-secret"}, "tencent": {"tencentcloud_secret_id": "secret-id", "tencentcloud_secret_key": "secret-value"}, "openai": {"openai_base_url": "https://user:pass@example.com/v1?token=hidden"}}})
        raw = store.path.read_text(encoding="utf-8")
        for value in ["not-a-real-secret-123", "deepl-secret", "secret-value", "secret-id", "user:pass", "token=hidden"]:
            assert value not in raw
        loaded = LocalStore(data)
        assert loaded.preferences["theme"] == "dark"
        assert loaded.preferences["engine_settings"]["openaicompatible"]["openai_compatible_model"] == "my-private-model"
        loaded.update({"engine": "bing"})
        api = DesktopApi(ROOT, ROOT, data, "0.2.0")
        api.update(file={"name": "sample.pdf", "path": str(data / "sample.pdf"), "pages": 12})
        assert api.validate_options({"engine": "bing", "page_mode": "custom", "pages": "1-3,2,8"})["page_count"] == 4
        assert not api.validate_options({"page_mode": "custom", "pages": "0,99"})["ok"]
        assert not api.validate_options({"page_mode": "custom", "pages": "6-2"})["ok"]
        assert api.validate_options({"page_mode": "first5"})["page_count"] == 5
        assert not api.start_translation({"engine": "deepl", "engine_settings": {}})["ok"]
        api.prepare_assets = lambda: None
        from pdf2zh_next import high_level
        original = high_level.do_translate_async_stream
        async def translated(settings, filename):
            assert settings.pdf.pages == "1-5"
            output = Path(settings.translation.output) / "translated.pdf"
            output.write_bytes(b"backend contract fixture")
            yield {"type": "progress_update", "overall_progress": 30, "stage": "Translate Paragraphs", "part_index": 1, "total_parts": 1}
            yield {"type": "finish", "translate_result": SimpleNamespace(mono_pdf_path=output, dual_pdf_path=None)}
        try:
            high_level.do_translate_async_stream = translated
            result = api.start_translation({"engine": "bing", "page_mode": "first5", "output": "mono"})
            assert result["ok"], result
            api.thread.join(15)
            state = api.get_state()
            assert state["status"] == "done", state
            assert state["task"]["trial"] and state["task"]["page_count"] == 5
            assert len(state["history"]) == 1
            api.save_preferences({"lang_out": "ja"})
            assert api.get_state()["task"]["lang_out"] == "zh"
            reopened = DesktopApi(ROOT, ROOT, data, "0.2.0")
            history = reopened.get_bootstrap()["history"]
            assert history[0]["lang_out"] == "zh" and history[0]["status"] == "done"
            output = Path(state["results"]["mono"])
            assert output.parent.parent == api.output_dir and output.read_bytes() == b"backend contract fixture"
            output.unlink()
            assert not reopened.open_history_result(history[0]["id"], "mono")["ok"]
            assert not reopened.open_history_result(history[0]["id"], "executable")["ok"]
            api.close()
        finally:
            high_level.do_translate_async_stream = original
    print("PASS: 23-service catalog, keyless Index, custom model mapping, secret redaction/persistence, exact page validation, task snapshot, history, missing results.")


if __name__ == "__main__":
    main()
