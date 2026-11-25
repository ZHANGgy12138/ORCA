"""Exercise the real WebView2 reader and translation bridge in an isolated app."""
import json
import os
from pathlib import Path
import time
import traceback


def run_reader_probe(window, bridge, api, chrome, data):
    report = {"ok": False}
    report_path = Path(os.environ.get("PDFMT_REPORT", data / "reader-test.json"))

    def wait(check, timeout=20, label="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.12)
        raise RuntimeError("Timed out: " + label)

    def js(script):
        return window.evaluate_js(script)

    def snapshot():
        return js("window.orcaReader.snapshot()")

    def select_paragraph(term, length=100):
        script = """(()=>{const layer=document.querySelector('.reader-page[data-page-number="1"] .textLayer');
            const spans=[...layer.querySelectorAll('span')].filter(s=>s.textContent.trim());
            let start=spans.findIndex(s=>s.textContent.includes(TERM));
            if(start<0)start=spans.findIndex(s=>/[A-Za-z]{4}/.test(s.textContent));let end=start,length=0;
            while(end<spans.length-1&&length<LENGTH){length+=spans[end].textContent.length;end++;}
            const range=document.createRange();range.setStart(spans[start].firstChild,0);range.setEnd(spans[end].firstChild,spans[end].textContent.length);
            const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
            layer.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));return selection.toString();})()"""
        started = time.monotonic()
        selected = js(script.replace("TERM", json.dumps(term)).replace("LENGTH", str(length)))
        wait(lambda: snapshot()["status"] in ("done", "error"), 45, "selection translation")
        current = snapshot()
        assert current["status"] == "done", current
        return {"selected": selected, "translation": current["translation"], "observed_ms": round((time.monotonic() - started) * 1000, 1), **current.get("metrics", {})}

    def capture(name):
        from System import Action
        from System.IO import MemoryStream
        from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
        stream = MemoryStream()
        holder = {}
        def begin():
            holder["task"] = window.native.webview.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream)
        window.native.Invoke(Action(begin))
        wait(lambda: holder["task"].IsCompleted, label="capture")
        holder["task"].GetAwaiter().GetResult()
        path = report_path.parent / name
        path.write_bytes(bytes(stream.ToArray()))
        stream.Dispose()
        return str(path)

    try:
        wait(lambda: js("window.__orcaReady === true && window.__orcaReaderReady === true"), 30, "reader initialization")
        input_path = os.environ["PDFMT_TEST_INPUT"]
        assert api.select_file(input_path)["ok"]
        original_queue = [f["id"] for f in api.get_state()["files"]]
        selected_engine = os.environ.get("PDFMT_TEST_ENGINE", "bing")
        js("document.getElementById('engine').value=" + json.dumps(selected_engine) + "; document.getElementById('engine').dispatchEvent(new Event('change')); document.getElementById('lang-in').value='en'; document.getElementById('lang-out').value='zh'")
        report["engine"] = selected_engine
        if selected_engine == "indextranslate":
            js("document.getElementById('settings-button').click()")
            report["no_api_key_required"] = js("document.querySelectorAll('#engine-fields input[type=password]').length===0 && document.getElementById('credential-storage').hidden")
            assert report["no_api_key_required"]
            js("document.getElementById('close-settings').click()")
        opened = bridge._reader.open_file(input_path)
        assert opened["ok"], opened
        js("window.__readerProbeError=null; window.orcaReader.openDocument(" + json.dumps(opened["document"]) + ").catch(e=>window.__readerProbeError=String(e))")
        wait(lambda: js("document.querySelectorAll('.reader-page[data-page-number=\"1\"] .textLayer span').length > 5 && !window.orcaReader.snapshot().rendering"), 40, "PDF rendering and text layer")
        report["page_count"] = snapshot()["pages"]
        assert report["page_count"] == opened["document"]["pages"]
        report["canvas"] = js("(()=>{const c=document.querySelector('.reader-page[data-page-number=\"1\"] canvas'); return {width:c.width,height:c.height,textSpans:document.querySelectorAll('.reader-page[data-page-number=\"1\"] .textLayer span').length}})()")
        assert report["canvas"]["width"] > 100 and report["canvas"]["textSpans"] > 5
        # Select real text-layer nodes; trigger the same mouseup path as the UI.
        first = select_paragraph("Wrist-Based")
        second = select_paragraph("Abstract:", 150)
        repeated = select_paragraph("Wrist-Based")
        report["timings"] = {"first": first, "second": second, "repeated": repeated}
        assert second.get("worker_reused"), second
        assert repeated.get("cache_hit"), repeated
        report["selection"] = repeated["selected"]
        assert len(report["selection"]) > 20
        wait(lambda: snapshot()["status"] in ("done", "error"), 45, "selection translation")
        result = snapshot()
        assert result["status"] == "done", result
        assert any("\u4e00" <= c <= "\u9fff" for c in result["translation"]), result
        report["translation"] = result["translation"]
        report["selection_translated"] = True
        report["queue_unchanged"] = [f["id"] for f in api.get_state()["files"]] == original_queue and api.get_state()["status"] == "idle"
        assert report["queue_unchanged"]
        if js("document.documentElement.dataset.theme") != "light":
            js("document.getElementById('theme-toggle').click()")
        time.sleep(0.3)
        report["light_screenshot"] = capture("reader-light.png")
        js("document.getElementById('theme-toggle').click()")
        time.sleep(0.3)
        report["dark_screenshot"] = capture("reader-dark.png")
        # Real scroll events, with no previous/next buttons. Multiple pages must
        # remain in the document and the page indicator follows the viewport.
        scroll_checks = []
        for page_number in (2, 5):
            js("(()=>{const v=document.getElementById('reader-viewport'),p=document.querySelector('.reader-page[data-page-number=\"" + str(page_number) + "\"]');v.scrollTop+=p.getBoundingClientRect().top-v.getBoundingClientRect().top-20;})()")
            wait(lambda: snapshot()["page"] == page_number and not snapshot()["rendering"], label="continuous scroll to page " + str(page_number))
            current = snapshot()
            assert current["translation"] == repeated["translation"]
            assert current["textLayers"] <= 5 + len(current["selection_pages"]), current
            scroll_checks.append({"page": current["page"], "scroll_top": current["scroll_top"], "rendered_pages": current["rendered_pages"]})
        report["continuous_scroll"] = scroll_checks
        js("(()=>{const v=document.getElementById('reader-viewport'),p=document.querySelector('.reader-page[data-page-number=\"5\"]');v.scrollTop+=Math.max(0,p.clientHeight-v.clientHeight*.55);})()")
        time.sleep(0.35)
        report["continuous_screenshot"] = capture("reader-continuous.png")
        before = snapshot()
        js("document.getElementById('window-floating').click()")
        wait(lambda: chrome.state()["floating"], label="enter floating")
        js("document.getElementById('floating-restore').click()")
        wait(lambda: not chrome.state()["floating"], label="leave floating")
        wait(lambda: not snapshot()["rendering"], label="restore reader rendering")
        report["floating_preserves_reading"] = snapshot()["document_id"] == before["document_id"] and snapshot()["translation"] == before["translation"] and snapshot()["page"] == before["page"]
        assert report["floating_preserves_reading"]
        js("document.getElementById('reader-next').click()")
        wait(lambda: snapshot()["page"] == 6 and not snapshot()["rendering"], label="next page")
        assert snapshot()["translation"] == repeated["translation"], snapshot()
        report["page_navigation"] = True
        window.resize(980, 720)
        time.sleep(0.6)
        report["compact_overflow"] = js("document.documentElement.scrollWidth > window.innerWidth")
        assert not report["compact_overflow"]
        report["compact_screenshot"] = capture("reader-compact.png")
        js("document.getElementById('reader-close').click()")
        wait(lambda: not snapshot()["document_id"], label="close document")
        report["closed_document"] = True
        report["ok"] = True
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
        try:
            report["reader_state"] = snapshot()
            report["browser_error"] = js("window.__readerProbeError")
            report["failure_screenshot"] = capture("reader-failed.png")
        except Exception:
            pass
    finally:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge._reader.close()
        api.close()
        window.destroy()
