"""Native window bridge and cancellable PDF translation jobs."""
import asyncio
import copy
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import uuid

STAGES = {
    "Parse PDF and Create Intermediate Representation": "读取文档",
    "DetectScannedFile": "检查文字与扫描页",
    "Parse Page Layout": "识别版面",
    "Parse Paragraphs": "整理段落",
    "Parse Formulas and Styles": "保留公式与样式",
    "Translate Paragraphs": "翻译正文",
    "Typesetting": "重新排版",
    "Add Fonts": "添加中文字体",
    "Generate drawing instructions": "生成页面",
    "Subset font": "整理字体",
    "Save PDF": "保存 PDF",
}


class DesktopApi:
    def __init__(self, root, portable_root, data, version):
        self.root, self.portable_root, self.data, self.version = root, portable_root, data, version
        self.window = None
        self.lock = threading.RLock()
        self.thread = self.loop = self.task = None
        self.cancel_requested = False
        self._closing = False
        self._testing_engine = False
        self._active_file_id = None
        self._batch_weights = {}
        from resume_store import ResumeStore
        self.recovery = ResumeStore(data)
        self.recovery.cleanup_abandoned()
        self._record = None
        self._pause_requested = False
        from preferences import LocalStore
        self.store = LocalStore(data)
        from credential_store import CredentialStore
        self.credentials = CredentialStore()
        self.output_dir = Path(self.store.preferences["output_dir"])
        self.state = {"status": "idle", "stage": "等待选择文件", "progress": 0, "file": None, "files": [], "batch": {"total": 0, "completed": 0, "failed": 0, "cancelled": 0, "current_index": 0, "progress": 0, "status": "idle"}, "results": {}, "error": None, "elapsed": 0, "part": "", "version": version, "output_dir": str(self.output_dir), "task": None, "activity": {"last_update_at": time.time()}}

    def get_bootstrap(self):
        from engine_registry import get_catalog
        with self.lock:
            return {"version": self.version, "preferences": copy.deepcopy(self.store.preferences), "engines": get_catalog(), "history": self.store.history_for_ui(), **self.list_recoveries()}

    def load_engine_credentials(self, engine, engine_settings):
        return self.credentials.load(engine, engine_settings)

    def save_engine_credentials(self, engine, engine_settings):
        return self.credentials.save(engine, engine_settings)

    def forget_engine_credentials(self, engine):
        return self.credentials.forget(engine)

    def save_preferences(self, patch):
        try:
            with self.lock:
                if self.is_busy():
                    patch = {"theme": patch.get("theme")}
                preferences = self.store.update(patch)
                self.output_dir = Path(preferences["output_dir"])
                self.state["output_dir"] = str(self.output_dir)
            return {"ok": True, "preferences": preferences}
        except Exception:
            return {"ok": False, "error": "设置暂时无法保存，请检查目录权限。"}

    def get_state(self):
        with self.lock:
            result = copy.deepcopy(self.state)
            if self.is_busy() and self.state.get("started_at"):
                result["elapsed"] = int(time.time() - self.state["started_at"])
            result["history"] = self.store.history_for_ui()
            result["activity"]["seconds_since_update"] = max(0, int(time.time() - result["activity"]["last_update_at"]))
            return result

    def is_busy(self):
        with self.lock:
            return self.state["status"] in ("preparing", "running", "pausing") or bool(self.thread and self.thread.is_alive())

    def update(self, **fields):
        with self.lock:
            if self._pause_requested and self.state["status"] == "pausing":
                fields.pop("stage", None)
            self.state.update(fields)
            self.state["activity"]["last_update_at"] = time.time()
            if self._active_file_id and "progress" in fields:
                item = self._find_file(self._active_file_id)
                if item:
                    item["progress"] = fields["progress"]
                self._sync_batch_locked()

    def _find_file(self, file_id):
        return next((item for item in self.state["files"] if item["id"] == file_id), None)

    def _sync_batch_locked(self):
        files = self.state["files"]
        weights = {item["id"]: self._batch_weights.get(item["id"], max(1, item["pages"])) for item in files}
        total_weight = sum(weights.values())
        consumed = sum(weights[item["id"]] * (1 if item["status"] in ("done", "error") else min(99, item["progress"]) / 100) for item in files)
        self.state["batch"].update(
            total=len(files), completed=sum(item["status"] == "done" for item in files),
            failed=sum(item["status"] == "error" for item in files),
            cancelled=sum(item["status"] == "cancelled" for item in files),
            progress=round(100 * consumed / total_weight, 1) if total_weight else 0,
            status=self.state["status"], page_count=total_weight,
        )

    def _queue_files_locked(self):
        # Preserve the existing single-file API used by integrations and tests.
        if not self.state["files"] and self.state.get("file"):
            item = copy.deepcopy(self.state["file"])
            item.update(id=item.get("id", uuid.uuid4().hex), status="ready", progress=0, error=None, results={})
            self.state["files"] = [item]
        return self.state["files"]

    @staticmethod
    def _inspect_pdf(filename):
        import pymupdf
        path = Path(filename).resolve(strict=True)
        if path.suffix.lower() != ".pdf":
            raise ValueError("请选择 PDF 文件。")
        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise ValueError("这份 PDF 需要密码，请先解密再打开。")
            if not len(document):
                raise ValueError("这份 PDF 没有可读取的页面。")
            return {"id": uuid.uuid4().hex, "path": str(path), "name": path.name,
                    "pages": len(document), "bytes": path.stat().st_size,
                    "has_text": any(document[i].get_text().strip() for i in range(min(3, len(document)))),
                    "status": "ready", "progress": 0, "error": None, "results": {}}

    def pick_file(self):
        if self.is_busy():
            return {"ok": False, "error": "请先完成或取消当前批次。"}
        import webview
        selected = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=("PDF 文档 (*.pdf)",))
        return self.select_files(selected, append=True) if selected else {"ok": False, "cancelled": True}

    def select_file(self, filename):
        result = self.select_files([filename], append=False)
        if result["ok"]:
            result["file"] = copy.deepcopy(self.state["file"])
        return result

    def select_files(self, filenames, append=True):
        if self.is_busy():
            return {"ok": False, "error": "当前批次尚未结束。"}
        if not isinstance(filenames, (list, tuple)):
            return {"ok": False, "error": "请选择一个或多个 PDF。"}
        candidates, rejected = [], []
        for filename in filenames:
            try:
                candidates.append(self._inspect_pdf(filename))
            except Exception as exc:
                rejected.append({"name": Path(str(filename)).name, "error": str(exc)})
        with self.lock:
            if self.is_busy():
                return {"ok": False, "error": "当前批次尚未结束。"}
            # Failed replacement must not discard a previously usable queue.
            if not candidates:
                return {"ok": False, "added": 0, "rejected": rejected, "files": copy.deepcopy(self.state["files"]), "error": "未导入有效 PDF，请检查文件是否损坏或需要密码。"}
            files = list(self.state["files"]) if append else []
            known = {os.path.normcase(item["path"]) for item in files}
            added = duplicates = 0
            for item in candidates:
                key = os.path.normcase(item["path"])
                if key in known:
                    duplicates += 1
                    continue
                files.append(item)
                known.add(key)
                added += 1
            self.state["files"] = files
            self._active_file_id = None
            self._batch_weights = {}
            self.state["batch"]["current_index"] = 0
            self.update(file=files[0] if files else None, status="idle", stage="已准备好", progress=0, results={}, error=None, elapsed=0, part="", task=None)
            self._sync_batch_locked()
            return {"ok": True, "added": added, "duplicates": duplicates, "rejected": rejected, "files": copy.deepcopy(files)}

    def remove_file(self, file_id):
        with self.lock:
            if self.is_busy():
                return {"ok": False, "error": "请先完成或取消当前批次。"}
            if not self._find_file(file_id):
                return {"ok": False, "error": "找不到这份文档。"}
            self.state["files"] = [item for item in self.state["files"] if item["id"] != file_id]
            self._selection_changed_locked()
            return {"ok": True, "files": copy.deepcopy(self.state["files"])}

    def clear_files(self):
        with self.lock:
            if self.is_busy():
                return {"ok": False, "error": "请先完成或取消当前批次。"}
            self.state["files"] = []
            self._selection_changed_locked()
            return {"ok": True, "files": []}

    def _selection_changed_locked(self):
        self._active_file_id = None
        self._batch_weights = {}
        self.state["batch"]["current_index"] = 0
        self.update(file=self.state["files"][0] if self.state["files"] else None, status="idle", stage="已准备好" if self.state["files"] else "等待选择文件", progress=0, results={}, error=None, elapsed=0, part="", task=None)
        self._sync_batch_locked()

    def pick_output(self):
        if self.is_busy():
            return {"ok": False, "error": "翻译进行时不能更改保存位置。"}
        import webview
        selected = self.window.create_file_dialog(webview.FileDialog.FOLDER)
        if selected:
            self.output_dir = Path(selected[0])
            self.update(output_dir=str(self.output_dir))
            self.store.update({"output_dir": str(self.output_dir)})
        return {"ok": True, "path": str(self.output_dir)}

    def _options(self, supplied):
        from engine_registry import engine_id
        if not isinstance(supplied, dict):
            raise ValueError("翻译设置格式无效。")
        options = copy.deepcopy(supplied)
        options["engine"] = engine_id(options.get("engine", self.store.preferences["engine"]))
        saved = self.store.preferences["engine_settings"].get(options["engine"], {})
        options["engine_settings"] = {**saved, **(options.get("engine_settings") or {})}
        for name in ("lang_in", "lang_out", "output", "page_mode"):
            options.setdefault(name, self.store.preferences[name])
        for name in ("auto_save", "translate_images"):
            options[name] = options.get(name) is True
        return options

    @staticmethod
    def _page_selection(options, file):
        total, mode = file["pages"], options["page_mode"]
        if mode == "all":
            return {"ok": True, "page_count": total, "pages": None, "pages_label": f"全文 · {total} 页"}
        if mode == "first5":
            count = min(5, total)
            return {"ok": True, "page_count": count, "pages": f"1-{count}", "pages_label": f"试译前 {count} 页"}
        if mode != "custom":
            return {"ok": False, "error": "请选择翻译范围。", "field": "page_mode"}
        ranges = options.get("pages", "").strip().replace("，", ",")
        if not ranges:
            return {"ok": False, "error": "请输入页码，例如 1-5 或 1-3,8。", "field": "pages"}
        selected = set()
        for part in ranges.split(","):
            match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
            if not match:
                return {"ok": False, "error": "页码格式无效，请使用 1-5 或 1-3,8。", "field": "pages"}
            start, end = int(match[1]), int(match[2] or match[1])
            if start < 1 or end < start or end > total:
                return {"ok": False, "error": f"{file['name']}：页码应在 1–{total} 之间，且起始页不大于结束页。", "field": "pages", "file_id": file.get("id")}
            selected.update(range(start, end + 1))
        count = len(selected)
        return {"ok": True, "page_count": count, "pages": ",".join(str(page) for page in sorted(selected)), "pages_label": f"指定页码 · {count} 页"}

    def validate_options(self, supplied):
        try:
            options = self._options(supplied)
            with self.lock:
                files = copy.deepcopy(self._queue_files_locked())
            if not files:
                return {"ok": False, "error": "请先选择 PDF。", "field": "file"}
            if options["output"] not in ("both", "mono", "dual"):
                return {"ok": False, "error": "请选择输出格式。", "field": "output"}
            if options["lang_in"] == options["lang_out"]:
                return {"ok": False, "error": "原文语言与译文语言不能相同。", "field": "lang_out"}
            if options["translate_images"] and options["engine"] != "indextranslate":
                return {"ok": False, "error": "图片文字翻译需要选择 Index-Translate 引擎。", "field": "engine"}
            selections = []
            for file in files:
                selection = self._page_selection(options, file)
                if not selection["ok"]:
                    return selection
                selections.append({**selection, "id": file["id"], "name": file["name"]})
            page_count = sum(row["page_count"] for row in selections)
            if len(selections) == 1:
                label = selections[0]["pages_label"]
            else:
                mode_label = "每份试译最多 5 页" if options["page_mode"] == "first5" else "全文" if options["page_mode"] == "all" else "各文档指定页码"
                label = f"{len(files)} 份文档 · {mode_label} · 共 {page_count} 页"
            return {"ok": True, "page_count": page_count, "pages_label": label, "pages": selections[0]["pages"] if len(selections) == 1 else None, "file_count": len(files), "per_file": selections}
        except Exception:
            return {"ok": False, "error": "请检查翻译设置。", "field": "engine"}

    def test_engine(self, supplied):
        from engine_registry import test_connection, redact, secret_values
        with self.lock:
            if self.is_busy() or self._testing_engine:
                return {"ok": False, "error": "请等待当前任务或连接测试结束。"}
            self._testing_engine = True
        options = {}
        try:
            options = self._options(supplied)
            return test_connection(options)
        except Exception as exc:
            return {"ok": False, "error": redact(exc, secret_values(options))}
        finally:
            with self.lock:
                self._testing_engine = False

    def prepare_assets(self):
        destination = self.data / "cache" / "babeldoc"
        source = self.root / "resources" / "babeldoc"
        for folder in ("fonts", "models", "cmap", "tiktoken"):
            origin = source / folder
            if not origin.is_dir():
                raise RuntimeError("资源文件缺失，请重新解压完整的软件压缩包。")
            for item in origin.rglob("*"):
                if item.is_file():
                    target = destination / folder / item.relative_to(origin)
                    if not target.exists() or target.stat().st_size != item.stat().st_size:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, target)

    def start_translation(self, options):
        return self._start_task(options, None)

    def _start_task(self, options, record):
        from engine_registry import build_engine, metadata, LABELS, redact, secret_values, safe_settings
        with self.lock:
            if self._closing:
                return {"ok": False, "error": "软件正在关闭。"}
            if self.is_busy() or self._testing_engine:
                return {"ok": False, "error": "当前任务尚未结束。"}
            try:
                options = options if record else self._options(options)
                validation = self.validate_options(options)
                if not validation["ok"]:
                    return validation
                build_engine(options)
            except Exception as exc:
                return {"ok": False, "error": redact(exc, secret_values(options)), "field": "engine"}
            meta = metadata()[options["engine"]]
            snapshot = {"engine_label": LABELS.get(meta.translate_engine_type, meta.translate_engine_type), "engine": options["engine"], "lang_in": options["lang_in"], "lang_out": options["lang_out"], "output": options["output"], "pages_label": validation["pages_label"], "page_count": validation["page_count"], "trial": options["page_mode"] == "first5"}
            snapshot.update(auto_save=options["auto_save"], translate_images=options["translate_images"])
            options["_snapshot"] = snapshot
            options["_output_dir"] = record["output_dir"] if record else str(self.output_dir)
            selections = {row["id"]: row for row in validation["per_file"]}
            files = copy.deepcopy(self.state["files"])
            for item in self.state["files"]:
                item.update(status="queued", progress=0, error=None, results={})
            for item in files:
                item["selection"] = selections[item["id"]]
            task_id = record["id"] if record else uuid.uuid4().hex
            self._record = record or self.recovery.create(task_id, options, files)
            self._was_recovery = bool(record)
            self._pause_requested = False
            if record:
                for saved in record["files"]:
                    if saved["status"] == "done":
                        self._find_file(saved["id"]).update(status="done", results=saved["results"], progress=100)
            if options["auto_save"]:
                try:
                    self.recovery.save(self._record, "active")
                except OSError:
                    return {"ok": False, "error": "无法建立自动保存记录，请检查磁盘空间与目录权限。"}
            self._batch_weights = {row["id"]: row["page_count"] for row in validation["per_file"]}
            try:
                self.store.update({**{name: options[name] for name in ("engine", "lang_in", "lang_out", "output", "page_mode")}, "engine_settings": {options["engine"]: safe_settings(options["engine"], options["engine_settings"])}})
            except OSError:
                pass
            self.cancel_requested = False
            self.state.update(status="preparing", progress=0, stage="准备翻译", error=None, results={}, part="", started_at=time.time(), elapsed=0, task=snapshot, task_id=task_id, activity={"last_update_at": time.time()})
            self.state["batch"]["current_index"] = 0
            self._sync_batch_locked()
            self.thread = threading.Thread(target=self._worker, args=(copy.deepcopy(options), files), daemon=True)
            self.thread.start()
        return {"ok": True, "file_count": len(files)}

    def list_recoveries(self):
        with self.lock:
            active = self.state.get("task_id") if self.is_busy() else None
            return {"ok": True, "recoveries": self.recovery.list(exclude=active)}

    def pause_and_save(self):
        with self.lock:
            if self._pause_requested and self.is_busy():
                return {"ok": True}
            if not self.is_busy() or self.cancel_requested or not self._record:
                return {"ok": False, "error": "当前没有可以暂停的翻译任务。"}
            self._pause_requested = True
            self.state.update(status="pausing", stage="正在保存翻译进度")
            self._sync_batch_locked()
            self._stop_current_task()
            return {"ok": True}

    def resume_saved(self, task_id, supplied=None):
        from resume_store import file_hash, options_for_resume
        with self.lock:
            if self.is_busy() or self._closing or self._testing_engine:
                return {"ok": False, "error": "请先结束当前任务。"}
            try:
                record = self.recovery.load(task_id)
                try:
                    options = options_for_resume(record, supplied or {}, self.credentials)
                except ValueError as error:
                    return {"ok": False, "field": "engine", "error": str(error)}
                files = []
                for saved in record["files"]:
                    try:
                        item = self._inspect_pdf(saved["path"])
                        if saved["sha256"] and file_hash(saved["path"]) != saved["sha256"]:
                            raise ValueError("原文件内容已改变。")
                    except (OSError, ValueError, RuntimeError):
                        return {"ok": False, "field": "source", "file_id": saved["id"], "error": "请重新选择原 PDF：" + saved["name"]}
                    if saved["status"] == "done":
                        for kind, path in saved["results"].items():
                            if not Path(path).is_file() or file_hash(path) != saved["result_hashes"].get(kind):
                                # Regenerate the missing output from saved work.
                                saved["status"] = "queued"
                                break
                    item["id"] = saved["id"]
                    files.append(item)
                self.state["files"] = files
                return self._start_task(options, record)
            except (OSError, ValueError, KeyError, TypeError):
                return {"ok": False, "error": "无法读取保存的任务，请检查任务文件和原 PDF。"}

    def relocate_saved(self, task_id, file_id):
        from resume_store import file_hash
        try:
            with self.lock:
                if self.is_busy():
                    return {"ok": False, "error": "请先结束当前任务。"}
                record = self.recovery.load(task_id)
                saved = next(file for file in record["files"] if file["id"] == file_id)
                import webview
                picked = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False, file_types=("PDF 文档 (*.pdf)",))
                if not picked:
                    return {"ok": False, "cancelled": True}
                path = Path(picked[0]).resolve(strict=True)
                if not saved["sha256"] or file_hash(path) != saved["sha256"]:
                    return {"ok": False, "error": "所选文件与保存时的 PDF 不一致。"}
                saved["path"], saved["name"] = str(path), path.name
                self.recovery.save(record, record["status"])
                return {"ok": True}
        except (OSError, ValueError, StopIteration):
            return {"ok": False, "error": "无法重新定位原 PDF。"}

    def discard_saved(self, task_id):
        with self.lock:
            if self.is_busy() and self.state.get("task_id") == task_id:
                return {"ok": False, "error": "当前任务正在运行。"}
            try:
                self.recovery.load(task_id)
                self.recovery.remove(task_id)
                return {"ok": True}
            except (OSError, ValueError):
                return {"ok": False, "error": "保存任务无法删除。"}

    def _record_file(self, file, status, results, elapsed, snapshot, created_at, task_id):
        history = {"id": task_id, "file_name": file["name"], "file_path": file["path"], "status": status,
                   "created_at": created_at, "engine_label": snapshot["engine_label"], "lang_out": snapshot["lang_out"],
                   "pages_label": snapshot["pages_label"], "results": results, "elapsed": elapsed}
        try:
            self.store.add_history(history)
        except OSError:
            logging.warning("Task history could not be saved.")

    def _worker(self, options, files):
        from engine_registry import secret_values, redact, SecretFilter
        from resume_store import atomic_json, file_hash
        record = self._record
        secrets = secret_values(options)
        secret_filter = SecretFilter(secrets)
        handlers = tuple(logging.getLogger().handlers)
        for handler in handlers:
            handler.addFilter(secret_filter)
        loop, fatal_error = None, None
        try:
            if not options["auto_save"] and not self._was_recovery:
                scratch = self.recovery.directory(record["id"])
                atomic_json(scratch / "scratch-owner.json", {"kind": "orca-temporary-task", "pid": os.getpid()})
            for saved in record["files"]:
                if not saved["sha256"] and Path(saved["path"]).is_file():
                    saved["sha256"] = file_hash(saved["path"])
            if options["auto_save"]:
                self.recovery.save(record, "active")
            self.prepare_assets()
            if self.cancel_requested or self._pause_requested:
                raise asyncio.CancelledError()
            from desktop_compat import install
            install()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            with self.lock:
                self.loop = loop
            for index, file in enumerate(files):
                saved = record["files"][index]
                if self._pause_requested or self.cancel_requested:
                    break
                if saved["status"] == "done":
                    continue
                started = time.time()
                file_task_id = file["id"] + "-" + record["id"]
                selection = file["selection"]
                snapshot = {**options["_snapshot"], "pages_label": selection["pages_label"], "page_count": selection["page_count"]}
                with self.lock:
                    self._active_file_id = file["id"]
                    item = self._find_file(file["id"])
                    restored_pages = self.recovery.completed_pages(record["id"], file["id"]) if self._was_recovery else 0
                    initial = min(99, int(100 * restored_pages / selection["page_count"]))
                    item.update(status="running", progress=initial, error=None, results={})
                    self.state.update(status="running", file=copy.deepcopy(item), progress=initial, stage="读取文档", part="", error=None, results={}, task=snapshot)
                    self.state["batch"]["current_index"] = index + 1
                    self._sync_batch_locked()
                status, error, outputs = "error", None, {}
                try:
                    folder = self.recovery.directory(record["id"]) / file["id"]
                    folder.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(dir=folder, prefix="work-", ignore_cleanup_errors=True) as work:
                        settings = self._settings_for_file(options, file, work)
                        with self.lock:
                            self._check_stopped()
                            self.task = loop.create_task(self._document_job(settings, file, options))
                        generated = loop.run_until_complete(self.task)
                        outputs, hashes = self._publish_results(options, file, generated)
                    saved.update(status="done", results=outputs, result_hashes=hashes)
                    status = "done"
                    if options["auto_save"]:
                        try:
                            self.recovery.save(record, "active")
                        except OSError:
                            logging.warning("Could not update checkpoint after publishing document.")
                except asyncio.CancelledError:
                    status = "paused" if self._pause_requested else "cancelled"
                    if not self._pause_requested:
                        self.cancel_requested = True
                except Exception as exc:
                    error = redact(exc, secrets)
                    logging.error("Translation failed for one document: %s", error)
                    if options["engine"] == "bing" and any(token in error for token in ("429", "403", "timed out")):
                        error = "免费翻译服务暂时限流或连接超时，请稍后重试或更换引擎。"
                finally:
                    with self.lock:
                        self.task = None
                        item = self._find_file(file["id"])
                        item.update(status=status, error=error, results=outputs)
                        if status == "done":
                            item["progress"] = 100
                        self.state["results"] = copy.deepcopy(outputs)
                        self._active_file_id = None
                        self._sync_batch_locked()
                    if status != "paused":
                        self._record_file(file, status, outputs, int(time.time() - started), snapshot, started, file_task_id)
                if self.cancel_requested or self._pause_requested:
                    break
        except asyncio.CancelledError:
            if not self._pause_requested:
                self.cancel_requested = True
        except Exception as exc:
            fatal_error = redact(exc, secrets)
            logging.error("Batch preparation failed: %s", fatal_error)
        finally:
            with self.lock:
                self.task = None
                self.loop = None
            try:
                if loop:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.close()
            finally:
                with self.lock:
                    # Pending documents are explicitly accounted for when stopped.
                    pending_status = "paused" if self._pause_requested else "cancelled" if self.cancel_requested else "error"
                    for file in files:
                        item = self._find_file(file["id"])
                        if item["status"] == "queued":
                            item.update(status=pending_status, error=fatal_error, results={})
                            snapshot = {**options["_snapshot"], "pages_label": file["selection"]["pages_label"]}
                            if pending_status != "paused":
                                self._record_file(file, pending_status, {}, 0, snapshot, time.time(), file["id"] + "-" + record["id"])
                    self._active_file_id = None
                    self._sync_batch_locked()
                    completed, failed = self.state["batch"]["completed"], self.state["batch"]["failed"]
                    if self._pause_requested and completed < len(files):
                        terminal_status, stage = "paused", "进度已保存，可以关闭 ORCA"
                    elif self.cancel_requested and completed < len(files):
                        terminal_status, stage = "cancelled", "批次已取消" if len(files) > 1 else "已取消"
                    elif failed:
                        terminal_status, stage = ("partial", "部分文档未完成") if completed else ("error", "翻译未完成")
                    else:
                        terminal_status, stage = "done", "全部翻译完成" if len(files) > 1 else "翻译完成"
                    error = fatal_error
                    if failed and not error:
                        error = f"{failed} 份文档未完成，可在队列中查看原因。" if len(files) > 1 else self.state["files"][0]["error"]
                    try:
                        if terminal_status == "paused":
                            for saved in record["files"]:
                                folder = self.recovery.directory(record["id"]) / saved["id"]
                                if (folder / "journal-error").exists() or ((folder / "journal-active").exists() and not (folder / "flush-confirmed.json").exists()):
                                    raise OSError("翻译片段保存失败。")
                            self.recovery.save(record, "paused")
                        elif (self._was_recovery or options["auto_save"]) and terminal_status in ("partial", "error"):
                            self.recovery.save(record, "interrupted")
                        else:
                            self.recovery.remove(record["id"])
                    except OSError:
                        if terminal_status == "paused":
                            terminal_status, stage = "error", "保存进度失败"
                            error = "无法完整保存当前进度，请检查磁盘空间；之前的自动保存记录仍可查看。"
                        else:
                            logging.warning("Could not update or clean task checkpoint.")
                    self.state.update(status=terminal_status, stage=stage, error=error, elapsed=int(time.time() - self.state["started_at"]))
                    if terminal_status == "done":
                        self.state["progress"] = 100
                    self._sync_batch_locked()
                    self.state["activity"]["last_update_at"] = time.time()
                for handler in handlers:
                    handler.removeFilter(secret_filter)

    async def _document_job(self, settings, file, options):
        outputs = await self._translate(settings, file["path"])
        if options["translate_images"]:
            from image_translation import append_image_translations
            directory = self.recovery.directory(self._record["id"]) / file["id"] / "images"
            return await append_image_translations(file["path"], outputs, file["selection"]["pages"],
                                                   options["lang_in"], options["lang_out"], directory,
                                                   lambda stage: self.update(stage=stage))
        return outputs

    def _check_stopped(self):
        if self.cancel_requested or self._pause_requested:
            raise asyncio.CancelledError()

    def _publish_results(self, options, file, generated):
        from resume_store import file_hash
        self._check_stopped()
        kinds = ("mono", "dual") if options["output"] == "both" else (options["output"],)
        sources = {}
        for kind in kinds:
            path = Path((generated or {}).get(kind) or "")
            if path.suffix.lower() != ".pdf" or not path.is_file() or not path.stat().st_size:
                raise RuntimeError("翻译未生成所选格式的完整 PDF，请重试或更换引擎。")
            sources[kind] = path
        if len({path.name.casefold() for path in sources.values()}) != len(kinds):
            raise RuntimeError("翻译结果文件名冲突，无法导出。")

        destination = Path(options["_output_dir"])
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / (Path(file["name"]).stem[:70] + "_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
        published = {kind: str(target / path.name) for kind, path in sources.items()}
        hashes = {}
        with tempfile.TemporaryDirectory(dir=destination, prefix=".orca-export-") as stage:
            for kind, source in sources.items():
                self._check_stopped()
                staged = Path(stage) / source.name
                try:
                    os.link(source, staged)
                except OSError:
                    # Different volumes and some USB/network filesystems require a copy.
                    checksum = hashlib.sha256()
                    with source.open("rb") as reader, staged.open("xb") as writer:
                        while block := reader.read(1024 * 1024):
                            self._check_stopped()
                            writer.write(block)
                            checksum.update(block)
                    hashes[kind] = checksum.hexdigest()
                else:
                    hashes[kind] = file_hash(staged)
            with self.lock:
                self._check_stopped()
                if os.path.lexists(target):
                    raise FileExistsError("结果目录已存在，请重试。")
                # A single same-volume rename makes all requested PDFs visible together.
                os.rename(stage, target)
        return published, hashes

    def _settings_for_file(self, options, file, target):
        from engine_registry import build_engine
        from resume_store import digest
        from pdf2zh_next.config.model import SettingsModel, BasicSettings, PDFSettings, TranslationSettings
        settings = SettingsModel(
            basic=BasicSettings(),
            translation=TranslationSettings(lang_in=options["lang_in"], lang_out=options["lang_out"], output=str(target), qps=1, pool_max_workers=1, no_auto_extract_glossary=True),
            pdf=PDFSettings(pages=file["selection"]["pages"], only_include_translated_page=bool(file["selection"]["pages"]), max_pages_per_part=50, no_mono=options["output"] == "dual", no_dual=options["output"] == "mono"),
            translate_engine_settings=build_engine(options),
        )
        settings.validate_settings()
        record = self._record
        directory = self.recovery.directory(record["id"]) / file["id"]
        from credential_store import _binding
        from engine_registry import metadata
        endpoint = _binding(options["engine"], metadata()[options["engine"]].setting_model_type.model_fields, options["engine_settings"])
        binding = digest({"settings": record["options"]["engine_settings"], "engine": options["engine"], "endpoint": endpoint, "omitted": record["omitted"]})
        settings._orca_checkpoint_spec = (str(directory), options["auto_save"], binding)
        return settings

    async def _translate(self, settings, filename):
        from pdf2zh_next.high_level import do_translate_async_stream
        stream = do_translate_async_stream(settings, filename)
        try:
            async for event in stream:
                if event["type"] == "finish":
                    result = event["translate_result"]
                    outputs = {key: str(path) for key, path in (("mono", result.mono_pdf_path), ("dual", result.dual_pdf_path)) if path and Path(path).is_file()}
                    if not outputs:
                        raise RuntimeError("翻译完成但未找到输出 PDF。")
                    self.update(stage="整理翻译结果", progress=99)
                    return outputs
                if event["type"] == "error":
                    raise RuntimeError(str(event.get("error", "翻译失败")))
                stage = event.get("stage", "")
                if stage:
                    translated_stage = next((value for key, value in STAGES.items() if stage.startswith(key)), "处理文档")
                    self.update(stage=translated_stage)
                if "overall_progress" in event:
                    self.update(progress=max(self.state["progress"], min(99, float(event["overall_progress"]))))
                if event.get("total_parts", 0) > 1:
                    self.update(part=f"第 {event['part_index']} / {event['total_parts']} 部分")
        finally:
            await stream.aclose()

    def _stop_current_task(self):
        loop, task = self.loop, self.task
        if loop and task and not task.done() and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    def cancel(self):
        with self.lock:
            if self.cancel_requested:
                return {"ok": True}
            self.cancel_requested = True
            if not self._pause_requested:
                self._stop_current_task()
        return {"ok": True}

    def open_queue_result(self, file_id, kind):
        with self.lock:
            item = self._find_file(file_id)
            value = (item or {}).get("results", {}).get(kind)
        if kind not in ("mono", "dual") or not value:
            return {"ok": False, "error": "这份文档没有对应的翻译结果。"}
        path = Path(value)
        if path.suffix.lower() != ".pdf" or not path.is_file():
            return {"ok": False, "error": "结果文件已移动或删除。"}
        os.startfile(str(path))
        return {"ok": True}

    def open_result(self, kind):
        path = self.state["results"].get(kind)
        if path and Path(path).is_file():
            os.startfile(path)
            return {"ok": True}
        return {"ok": False, "error": "结果文件不存在，请重新翻译。"}

    def open_history_result(self, task_id, kind):
        with self.store.lock:
            row = next((item for item in self.store.history if item["id"] == task_id), None)
        value = (row or {}).get("results", {}).get(kind)
        if kind not in ("mono", "dual") or not value:
            return {"ok": False, "error": "这项任务没有对应的结果。"}
        path = Path(value)
        if path.suffix.lower() != ".pdf" or not path.is_file():
            return {"ok": False, "error": "结果文件已移动或删除。"}
        os.startfile(str(path))
        return {"ok": True}

    def select_history(self, task_id):
        with self.store.lock:
            row = next((item for item in self.store.history if item["id"] == task_id), None)
        if not row:
            return {"ok": False, "error": "找不到这项任务。"}
        return self.select_file(row.get("file_path", ""))

    def open_output(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.output_dir))
        return {"ok": True}

    def open_source(self):
        path = self.portable_root / "SOURCE.zip"
        if not path.exists():
            path = self.portable_root / "README.md"
        if path.exists():
            os.startfile(str(path))
        return {"ok": True}

    def close(self):
        with self.lock:
            self._closing = True
            if self.is_busy() and self._record and self._record["options"].get("auto_save") and not self.cancel_requested:
                self.pause_and_save()
            elif not self._pause_requested:
                self.cancel()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=8)
