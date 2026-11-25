"""Small atomic store; only approved nonsecret settings and task history."""
import copy
import json
import os
from pathlib import Path
import threading
import uuid
from filelock import FileLock

HISTORY_FIELDS = {"id", "file_name", "file_path", "status", "created_at", "engine_label", "lang_out", "pages_label", "results", "elapsed"}


class LocalStore:
    def __init__(self, data):
        self.path = Path(data) / "preferences.v2.json"
        self.lock = threading.RLock()
        self.file_lock = FileLock(str(self.path) + ".lock", timeout=10)
        self.preferences = {"theme": "light", "lang_in": "en", "lang_out": "zh", "output": "both", "engine": "bing", "page_mode": "all", "output_dir": str(Path(data) / "output"), "engine_settings": {}}
        self.history = []
        try:
            content = json.loads(self.path.read_text(encoding="utf-8"))
            self.update(content.get("preferences", {}), write=False)
            self.history = self._merge_history(content.get("history", []))
        except (OSError, ValueError, TypeError):
            pass

    def update(self, patch, write=True):
        if not isinstance(patch, dict):
            raise ValueError("设置格式无效。")
        with self.lock:
            if write:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.file_lock:
                    self._refresh_locked()
                    self._apply_patch(patch)
                    self._write_locked()
            else:
                self._apply_patch(patch)
            return copy.deepcopy(self.preferences)

    def _apply_patch(self, patch):
        from engine_registry import engine_id, safe_settings
        if not isinstance(patch, dict):
            raise ValueError("设置格式无效。")
        with self.lock:
            for name, choices in {"theme": ("light", "dark"), "output": ("both", "mono", "dual"), "page_mode": ("all", "first5", "custom")}.items():
                if patch.get(name) in choices:
                    self.preferences[name] = patch[name]
            for name in ("lang_in", "lang_out"):
                if patch.get(name) in ("en", "zh", "zh-tw", "ja", "de", "fr", "es", "ko", "ru", "it", "pt"):
                    self.preferences[name] = patch[name]
            if patch.get("engine"):
                self.preferences["engine"] = engine_id(patch["engine"])
            if isinstance(patch.get("output_dir"), str) and Path(patch["output_dir"]).is_absolute():
                self.preferences["output_dir"] = patch["output_dir"]
            profiles = patch.get("engine_settings", {})
            if isinstance(profiles, dict):
                for provider, values in profiles.items():
                    try:
                        canonical = engine_id(provider)
                        cleaned = safe_settings(canonical, values) if isinstance(values, dict) else {}
                        self.preferences["engine_settings"].setdefault(canonical, {}).update(cleaned)
                    except (ValueError, TypeError):
                        continue

    @staticmethod
    def _merge_history(*collections):
        rows = {}
        for collection in collections:
            for row in collection if isinstance(collection, list) else []:
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    rows[row["id"]] = {k: copy.deepcopy(v) for k, v in row.items() if k in HISTORY_FIELDS}
        def timestamp(row):
            try:
                return float(row.get("created_at", 0))
            except (TypeError, ValueError):
                return 0
        return sorted(rows.values(), key=timestamp, reverse=True)[:20]

    def _refresh_locked(self):
        """Refresh before writing so another ORCA process cannot lose its work."""
        try:
            content = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(content, dict):
            return
        if isinstance(content.get("preferences"), dict):
            self._apply_patch(content["preferences"])
        self.history = self._merge_history(self.history, content.get("history", []))

    def _write_locked(self):
        temporary = self.path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps({"preferences": self.preferences, "history": self.history}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.file_lock:
                self._refresh_locked()
                self._write_locked()

    def add_history(self, row):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.file_lock:
                self._refresh_locked()
                self.history = self._merge_history(self.history, [row])
                self._write_locked()

    def history_for_ui(self):
        with self.lock:
            # Atomic replacement makes this read safe without blocking a UI poll
            # on another process's preference write.
            try:
                content = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(content, dict):
                    self.history = self._merge_history(self.history, content.get("history", []))
            except (OSError, ValueError):
                pass
            return [{k: copy.deepcopy(v) for k, v in row.items() if k != "file_path"} for row in self.history]
