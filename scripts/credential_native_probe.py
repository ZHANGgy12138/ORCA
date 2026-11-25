"""Two real app launches using only isolated, fake Windows credentials."""
import json
import os
from pathlib import Path
import re
import time

FAKE_FIRST = "orca-ui-test-only-first-key"
FAKE_SECOND = "orca-ui-test-only-replaced-key"


def test_namespace():
    value = os.environ.get("ORCA_TEST_CREDENTIAL_NAMESPACE", "")
    if not re.fullmatch(r"ORCA/credential-test/[0-9a-f]{32}", value):
        raise ValueError("An isolated credential-test namespace is required")
    return value


def run_credential_probe(window, bridge, api, data, stage):
    report = {"ok": False, "stage": stage, "real_windows_vault": True}
    path = Path(os.environ.get("PDFMT_REPORT", data / ("credential-stage-" + str(stage) + ".json")))

    def js(code):
        return window.evaluate_js(code)

    def wait(check, label):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(.1)
        raise AssertionError("Timed out: " + label)

    def field(value):
        js("(()=>{const el=document.getElementById('setting-deepl_auth_key');el.value=" + json.dumps(value) + ";el.dispatchEvent(new Event('input',{bubbles:true}));})()")

    def apply():
        js("document.getElementById('apply-settings').click()")
        wait(lambda: js("!document.getElementById('settings-dialog').open"), "apply settings")

    try:
        wait(lambda: js("window.__orcaReady===true"), "bootstrap")
        js("document.getElementById('settings-button').click()")
        if stage == 1:
            js("document.getElementById('engine').value='deepl';document.getElementById('engine').dispatchEvent(new Event('change'))")
            wait(lambda: js("!!document.getElementById('setting-deepl_auth_key') && !document.getElementById('apply-settings').disabled"), "DeepL controls")
            assert not js("document.getElementById('remember-credentials').checked")
            field(FAKE_FIRST)
            apply()
            assert not api.credentials.load("deepl", {})["remembered"]
            report["default_session_only"] = True
            js("document.getElementById('settings-button').click();document.getElementById('remember-credentials').checked=true;document.getElementById('remember-credentials').dispatchEvent(new Event('change'))")
            apply()
            saved = api.credentials.load("deepl", {})
            assert saved["ok"] and saved["secrets"]["deepl_auth_key"] == FAKE_FIRST
            wait(lambda: api.store.preferences.get("engine") == "deepl", "persist selected engine")
            report["explicit_opt_in_saved"] = True
        else:
            wait(lambda: js("document.getElementById('engine').value==='deepl' && document.getElementById('setting-deepl_auth_key')?.value===" + json.dumps(FAKE_FIRST)), "restart autofill")
            assert js("document.getElementById('remember-credentials').checked && document.getElementById('setting-deepl_auth_key').type==='password'")
            report["restart_autofill_masked"] = True
            field(FAKE_SECOND)
            apply()
            assert api.credentials.load("deepl", {})["secrets"]["deepl_auth_key"] == FAKE_SECOND
            report["replace_saved_key"] = True
            js("document.getElementById('settings-button').click();document.getElementById('forget-credentials').click()")
            wait(lambda: js("document.getElementById('setting-deepl_auth_key').value==='' && !document.getElementById('remember-credentials').checked"), "forget cleared UI")
            assert not api.credentials.load("deepl", {})["remembered"]
            report["forget_deleted_vault_and_input"] = True
        # Never emit a credential value in the report, including fake values.
        time.sleep(.5)
        for file in data.rglob("*"):
            if file.is_file() and file.suffix in (".json", ".log", ".toml"):
                content = file.read_text(encoding="utf-8", errors="ignore")
                assert FAKE_FIRST not in content and FAKE_SECOND not in content, "Credential in application file"
        report["no_plaintext_in_app_files"] = True
        report["ok"] = True
    except Exception as exc:
        report["error"] = str(exc).replace(FAKE_FIRST, "[test-key]").replace(FAKE_SECOND, "[test-key]")
    finally:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge._reader.close()
        api.close()
        window.destroy()
