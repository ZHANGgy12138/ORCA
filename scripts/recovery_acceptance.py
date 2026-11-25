"""Restart the real app twice per scenario using an authenticated loopback Dify fixture.

Run with the build venv: python scripts/recovery_acceptance.py --exe path/to/ORCA.exe
Use --source to run the same cases against app.py. Only the test's own process
tree is terminated. The controller never opens a desktop window or a user PDF.
"""
import argparse
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
PAGE_COUNT = 3
WORDS = ("amber birch coral dawn elm frost grove hazel ivory jade kelp lilac "
         "maple north olive").split()


def make_pdf(path):
    import pymupdf
    with pymupdf.open() as pdf:
        for page_index in range(PAGE_COUNT):
            page = pdf.new_page(width=500, height=650)
            for row in range(5):
                word = WORDS[5 * page_index + row]
                top = 26 + row * 120
                paragraph = (f"The {word} team prepared a {path.stem} report on the changing tides. "
                             "The observers took their readings throughout the day and checked the water "
                             "level at regular intervals. Each observation was compared with the previous "
                             "survey so that differences could be identified and explained.")
                space = page.insert_textbox((30, top, 470, top + 110), paragraph, fontsize=12)
                assert space >= 0
        pdf.save(path)


def wait_for(check, detail):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.1)
    raise TimeoutError(detail)


def recoveries(api):
    result = api.list_recoveries()
    assert result["ok"], result
    return result["recoveries"]


def run_probe(root, portable_root, data, version):
    """Run inside ORCA. Restrict this diagnostic to its generated test directory."""
    from credential_store import CredentialStore
    from desktop_api import DesktopApi
    from resume_store import atomic_json

    token = os.environ.get("ORCA_RECOVERY_TOKEN", "")
    scenario = os.environ.get("ORCA_RECOVERY_CASE")
    phase = os.environ.get("ORCA_RECOVERY_PHASE")
    if (not re.fullmatch("[0-9a-f]{32}", token) or scenario not in ("manual", "power")
            or phase not in ("start", "resume") or data.name != "orca-recovery-" + token):
        return 2
    api = DesktopApi(root, portable_root, data, version)
    api.credentials = CredentialStore("ORCA/credential-test/" + token)
    key = os.environ["ORCA_RECOVERY_FAKE_KEY"]
    port = int(os.environ["ORCA_RECOVERY_PORT"])
    options = {"engine": "dify", "engine_settings": {
        "dify_url": f"http://127.0.0.1:{port}/v1/workflows/run", "dify_apikey": key},
        "lang_in": "en", "lang_out": "zh", "output": "mono", "page_mode": "all",
        "auto_save": scenario == "power", "translate_images": False}
    report = {"version": version, "frozen": bool(getattr(sys, "frozen", False))}
    try:
        if phase == "start":
            source = data / (scenario + ".pdf")
            make_pdf(source)
            assert api.select_file(str(source))["ok"]
            started = api.start_translation(options)
            assert started["ok"], started
            def gate_reached():
                if not api.is_busy():
                    raise RuntimeError(str(api.get_state().get("error") or "Translation finished before the interruption"))
                return (data / (scenario + ".gate")).exists()
            wait_for(gate_reached, "Local engine interruption point was not reached")
            if scenario == "power":
                # The controller terminates this process tree once committed responses exist.
                wait_for(lambda: not api.is_busy(), "Controller did not interrupt the process")
                raise AssertionError("Automatic scenario unexpectedly completed")
            assert not list((data / "resume-v1").glob("*/manifest.json")), "Unexpected automatic save in manual mode"
            paused = api.pause_and_save()
            assert paused["ok"], paused
            wait_for(lambda: not api.is_busy(), "Manual pause timed out")
            assert api.get_state()["status"] == "paused", api.get_state().get("error")
        else:
            saved = recoveries(api)
            assert len(saved) == 1, saved
            task = saved[0]
            assert task["engine"] == "dify" and task["auto_save"] == options["auto_save"], task
            assert task["saved_segments"] > 0 and task["total_pages"] == PAGE_COUNT, task
            report["saved_segments"] = task["saved_segments"]
            started = api.resume_saved(task["id"], options)
            assert started["ok"], started
            wait_for(lambda: not api.is_busy(), "Resumed translation timed out")
            state = api.get_state()
            assert state["status"] == "done", state.get("error")
            output = state["files"][0]["results"]["mono"]
            import pymupdf
            with pymupdf.open(output) as pdf:
                assert len(pdf) == PAGE_COUNT
                assert all("".join(page.get_text().split()).count("潮汐记录") == 5 for page in pdf), "Missing translated paragraphs"
            assert not recoveries(api), "Completed recovery still offered"
            report["output"] = output
        report["ok"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}".replace(key, "[redacted]")
    finally:
        api.close()
        atomic_json(data / f"{scenario}-{phase}.json", report)
    return 0 if report.get("ok") else 1


def request_key(text):
    return hashlib.sha256(json.dumps(text, ensure_ascii=False).encode()).hexdigest()


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        service = self.server
        if self.path != "/v1/workflows/run" or self.headers.get("Authorization") != "Bearer " + service.key:
            self.send_error(403)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = body["inputs"]["text"]
        if text != "Hello":
            with service.lock:
                service.requests.append(request_key(text))
                gated = service.limit and len(service.requests) >= service.limit
            if gated:
                service.gate.touch()
                if not service.release.wait(120):
                    self.send_error(504)
                    return
        translated = "潮汐记录" + ("甲乙丙丁戊己庚辛壬癸"[int(request_key(text)[0], 16) % 10])
        payload = json.dumps({"data": {"outputs": {"text": translated}}}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass  # The interrupted worker may already have closed its socket.


def stored_keys(data):
    keys = set()
    for path in (data / "resume-v1").glob("*/*/segments.sqlite"):
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.5)) as conn:
            keys.update(key for key, in conn.execute("select key from responses"))
    return keys


def check_secrets(data, key):
    # Fonts and models are immutable distribution assets. All generated files
    # (including SQLite journals/WALs, PDFs and logs) are checked as bytes.
    signatures = [key.encode(), key.encode("utf-16le")]
    for path in data.rglob("*"):
        assets = data / "cache" / "babeldoc"
        if not path.is_file() or any(assets / name in path.parents for name in ("fonts", "models", "cmap", "tiktoken")):
            continue
        with path.open("rb") as stream:
            tail = b""
            while block := stream.read(262144):
                block = tail + block
                assert not any(value in block for value in signatures), f"Credential appeared in {path.name}"
                tail = block[-max(map(len, signatures)):]


def kill_test_tree(process):
    """Stop only descendants of the specific Popen handle, including frozen workers."""
    import psutil
    if process.poll() is not None:
        return
    root = psutil.Process(process.pid)
    root.suspend()
    children = root.children(recursive=True)
    for child in children:
        try:
            child.suspend()
        except psutil.NoSuchProcess:
            pass
    process.kill()
    for child in reversed(children):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    process.wait(15)
    _, remaining = psutil.wait_procs(children, timeout=10)
    assert not remaining, "A probe worker survived termination"


def run_case(command, data, environment, scenario):
    service = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    service.key = environment["ORCA_RECOVERY_FAKE_KEY"]
    service.lock = threading.Lock()
    service.release = threading.Event()
    service.gate = data / (scenario + ".gate")
    service.limit = 12 if scenario == "power" else 3
    service.requests = []
    threading.Thread(target=service.serve_forever, daemon=True).start()
    env = {**environment, "ORCA_RECOVERY_CASE": scenario, "ORCA_RECOVERY_PORT": str(service.server_port)}
    def launch(phase):
        return subprocess.Popen([*command, "--recovery-probe"], env={**env, "ORCA_RECOVERY_PHASE": phase},
                                cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    def result(phase):
        report = json.loads((data / f"{scenario}-{phase}.json").read_text(encoding="utf-8"))
        assert report.get("ok"), report
        return report
    process = None
    try:
        process = launch("start")
        def interrupted():
            if process.poll() is not None:
                result("start")
                return service.gate.exists()
            return service.gate.exists()
        wait_for(interrupted, "No request reached the fixture interruption point")
        if scenario == "power":
            wait_for(lambda: len(stored_keys(data)) >= 10, "No batch of responses was durably saved")
            kill_test_tree(process)
        else:
            # Keep the HTTP request blocked while the real application handles
            # pause/save, so pending responses cannot slip into the checkpoint.
            process.wait(45)
            result("start")
        saved = stored_keys(data)
        assert saved and saved <= set(service.requests), "Checkpoint must contain completed fixture requests"
        check_secrets(data, service.key)

        # Remove the fallback shared cache from the lookup path. This forces
        # reuse to come from the application's own durable task journal.
        cache = data / "cache" / "translation"
        if cache.exists():
            cache.rename(cache.with_name("previous-" + scenario))
        service.limit = 0
        service.release.set()
        service.requests = []
        process = launch("resume")
        process.wait(220)
        report = result("resume")
        assert report["frozen"] == (len(command) == 1)
        assert service.requests, "Unfinished work was not submitted on resume"
        assert not saved.intersection(service.requests), "A committed request was sent again"
        check_secrets(data, service.key)
        return {**report, "journal_entries_reused": len(saved), "remaining_requests": len(service.requests)}
    finally:
        if process is not None and process.poll() is None:
            kill_test_tree(process)
        service.release.set()
        service.shutdown()
        service.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    launch = parser.add_mutually_exclusive_group(required=True)
    launch.add_argument("--source", action="store_true")
    launch.add_argument("--exe", type=Path)
    parser.add_argument("--case", choices=("manual", "power", "both"), default="both")
    args = parser.parse_args()
    command = [str(args.exe.resolve(strict=True))] if args.exe else [sys.executable, str(ROOT / "app.py")]
    token = uuid.uuid4().hex
    data = ROOT / "test-data" / ("orca-recovery-" + token)
    data.mkdir(parents=True)
    no_proxy = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1", "localhost"]))
    env = {**os.environ, "PDFMT_DATA_DIR": str(data), "ORCA_RECOVERY_TOKEN": token,
           "ORCA_RECOVERY_FAKE_KEY": "local-test-" + uuid.uuid4().hex,
           "NO_PROXY": no_proxy, "no_proxy": no_proxy, "PYTHONUTF8": "1"}
    if args.source:
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "vendor"), str(ROOT)])
    print(f"Isolated recovery test: {data}", flush=True)
    cases = ("manual", "power") if args.case == "both" else (args.case,)
    reports = {}
    for case in cases:
        reports[case] = run_case(command, data, env, case)
        print(case, json.dumps(reports[case], ensure_ascii=False), flush=True)
    (data / "acceptance.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
