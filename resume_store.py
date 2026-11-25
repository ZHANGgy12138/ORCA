"""Atomic task manifests, validated PDF parts and a per-document response journal."""
from dataclasses import asdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def child(root, name):
    if not isinstance(name, str) or not re.fullmatch('[0-9a-f]{32}', name):
        raise ValueError('保存任务标识无效。')
    root = Path(root).resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise ValueError('任务目录无效。')
    return path


def engine_snapshot(options):
    from engine_registry import safe_settings, is_secret
    settings = options.get('engine_settings', {})
    safe = safe_settings(options['engine'], settings)
    # Secret or signed endpoints have to be re-entered, never persisted.
    omitted = {key: digest(value) for key, value in settings.items() if key not in safe and not is_secret(key) and value}
    return safe, omitted


def options_for_resume(record, supplied, vault):
    from engine_registry import is_secret, metadata, build_engine, engine_id
    options = dict(record['options'])
    engine = options['engine']
    config = dict(options['engine_settings'])
    incoming = supplied.get('engine_settings', {}) if isinstance(supplied, dict) and supplied.get('engine') and engine_id(supplied['engine']) == engine else {}
    if not isinstance(incoming, dict):
        raise ValueError('翻译服务设置格式无效。')
    for name, checksum in record.get('omitted', {}).items():
        if name not in incoming or digest(incoming[name]) != checksum:
            raise ValueError('请重新填写保存任务时使用的接口或受保护的设置。')
        config[name] = incoming[name]
    names = [name for name in metadata()[engine].setting_model_type.model_fields if is_secret(name)]
    if names:
        saved = vault.load(engine, config)
        config.update(saved.get('secrets') or {})
        # A supplied key must never be sent to an endpoint different from the
        # one for which it was entered. Compare the same binding as CredMan.
        from credential_store import _binding
        fields = metadata()[engine].setting_model_type.model_fields
        if _binding(engine, fields, config) == _binding(engine, fields, incoming):
            config.update({name: incoming[name] for name in names if incoming.get(name)})
    options['engine_settings'] = config
    build_engine(options)
    return options


class ResumeStore:
    def __init__(self, data):
        self.root = Path(data) / 'resume-v1'

    def cleanup_abandoned(self):
        # Only remove marked ephemeral work after its owner has exited and
        # a full day has elapsed. Other ORCA data, outputs and any manifest
        # (including an unreadable one) are never eligible.
        if not self.root.is_dir():
            return
        for owner in self.root.glob('*/scratch-owner.json'):
            try:
                folder = self.directory(owner.parent.name)
                if (folder/'manifest.json').exists() or time.time() - owner.stat().st_mtime < 86400:
                    continue
                identity = json.loads(owner.read_text(encoding='utf-8'))
                import psutil
                if identity.get('kind') == 'orca-temporary-task' and not psutil.pid_exists(int(identity['pid'])):
                    self.remove(folder.name)
            except (OSError, ValueError, KeyError):
                continue

    def directory(self, task_id):
        return child(self.root, task_id)

    def create(self, task_id, options, files):
        safe, omitted = engine_snapshot(options)
        allowed = ('engine', 'lang_in', 'lang_out', 'output', 'page_mode', 'pages', 'translate_images', 'auto_save')
        clean = {name: options[name] for name in allowed if name in options}
        clean['engine_settings'] = safe
        # Never copy transient state, errors or supplied arbitrary arguments.
        items = []
        for file in files:
            item = {key: file[key] for key in ('id', 'path', 'name', 'pages', 'selection')}
            item.update(status='queued', sha256=None, results={}, result_hashes={})
            items.append(item)
        return dict(schema=1, id=task_id, created_at=time.time(), updated_at=time.time(), options=clean, omitted=omitted,
                    output_dir=options['_output_dir'], engine_label=options['_snapshot']['engine_label'], status='active', files=items)

    def save(self, record, status):
        record['status'] = status
        record['updated_at'] = time.time()
        atomic_json(self.directory(record['id']) / 'manifest.json', record)

    def load(self, task_id):
        record = json.loads((self.directory(task_id) / 'manifest.json').read_text(encoding='utf-8'))
        if record.get('schema') != 1 or record.get('id') != task_id or not record.get('files'):
            raise ValueError('保存记录损坏。')
        for file in record['files']:
            child(self.directory(task_id), file['id'])
        return record

    def remove(self, task_id):
        directory = self.directory(task_id)
        if directory.exists():
            # Remove the user's recovery entry first. A locked intermediate
            # file on Windows can delay cleanup without resurrecting the job.
            manifest = directory / 'manifest.json'
            if manifest.is_file():
                atomic_json(directory / 'scratch-owner.json', {'kind': 'orca-temporary-task', 'pid': os.getpid()})
                manifest.unlink()
            shutil.rmtree(directory)

    def completed_pages(self, task_id, file_id):
        folder = child(self.directory(task_id), file_id)
        return sum(int(json.loads(p.read_text(encoding='utf-8'))['pages']) for p in folder.glob('part-*.json'))

    def list(self, exclude=None):
        result = []
        if not self.root.is_dir():
            return result
        for path in self.root.glob('*/manifest.json'):
            try:
                if path.parent.name == exclude:
                    continue
                record = self.load(path.parent.name)
                pages = segments = 0
                latest = record['updated_at']
                for file in record['files']:
                    folder = child(path.parent, file['id'])
                    markers = list(folder.glob('part-*.json'))
                    if file['status'] == 'done':
                        pages += file['selection']['page_count']
                    else:
                        for marker in markers:
                            pages += int(json.loads(marker.read_text(encoding='utf-8'))['pages'])
                            latest = max(latest, marker.stat().st_mtime)
                    db = folder/'segments.sqlite'
                    if db.exists():
                        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=.3)) as conn:
                            segments += conn.execute('select count(*) from responses').fetchone()[0]
                        latest = max(latest, db.stat().st_mtime)
                files = record['files']
                result.append(dict(id=record['id'], file_name=files[0]['name'], file_count=len(files), engine=record['options']['engine'],
                                   engine_label=record['engine_label'], saved_pages=pages, saved_segments=segments,
                                   total_pages=sum(f['selection']['page_count'] for f in files), updated_at=latest,
                                   status=record['status'], auto_save=record['options'].get('auto_save', False), translate_images=record['options'].get('translate_images', False)))
            except (OSError, ValueError, TypeError, KeyError, IndexError, sqlite3.Error):
                continue
        return sorted(result, key=lambda row: row['updated_at'], reverse=True)


class ResponseJournal:
    """Per-document exact-request cache. Flushes in batches only when armed."""
    def __init__(self, directory, auto_save, previous):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'segments.sqlite'
        self.previous = previous
        self.auto_save = auto_save
        self.pending = {}
        self.lock = threading.Lock()
        self.last_flush = time.monotonic()
        self.db = None
        self.failed = False
        self.flushing = False
        if self.path.is_file():
            self._open()

    def _open(self):
        if self.db is None:
            self.db = sqlite3.connect(self.path, check_same_thread=False)
            self.db.execute('pragma synchronous=FULL')
            self.db.execute('create table if not exists responses (key text primary key, value text not null)')
            self.db.commit()
        return self.db

    def _remember(self, key, value):
        self.pending[key] = value
        if self.flushing or (self.auto_save and len(self.pending) >= 10):
            self._flush()

    def get(self, text):
        key = digest(text)
        with self.lock:
            if key in self.pending:
                return self.pending[key]
            if self.db:
                row = self.db.execute('select value from responses where key=?', (key,)).fetchone()
                if row:
                    return row[0]
        result = self.previous.get(text)
        if result is not None:
            with self.lock:
                self._remember(key, result)
        return result

    def set(self, text, value):
        with self.lock:
            self._remember(digest(text), value)
        self.previous.set(text, value)

    def _flush(self):
        if self.pending:
            try:
                with self._open() as db:
                    db.executemany('insert or replace into responses values (?, ?)', self.pending.items())
                self.pending.clear()
            except (OSError, sqlite3.Error):
                self.failed = True
                (self.directory/'journal-error').touch()
                raise
        self.last_flush = time.monotonic()

    def flush(self, force=False):
        with self.lock:
            if force or (self.auto_save and time.monotonic() - self.last_flush >= 10):
                self._flush()

    def seal(self):
        """Commit returned translations; any late worker results commit directly."""
        with self.lock:
            self.flushing = True
            self._flush()
            if self.failed:
                raise OSError('保存翻译片段失败。')
            atomic_json(self.directory/'flush-confirmed.json', {'ok': True})


class FileCheckpoint:
    def __init__(self, directory, auto_save):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.auto_save = auto_save
        self.journal = None

    def install(self, translator, binding):
        translator.add_cache_impact_parameters('orca_profile', binding)
        self.journal = ResponseJournal(self.directory, self.auto_save, translator.cache)
        translator.cache = self.journal
        (self.directory/'flush-confirmed.json').unlink(missing_ok=True)
        (self.directory/'journal-error').unlink(missing_ok=True)
        (self.directory/'journal-active').touch()

    def check(self):
        if self.journal.failed:
            raise OSError('保存翻译片段失败，请检查磁盘空间。')

    def tick(self):
        self.journal.flush()
        self.check()

    def _marker(self, index):
        return self.directory / f'part-{int(index)}.json'

    def part_output(self, index):
        out = self.directory / f'part-{int(index)}'
        out.mkdir(parents=True, exist_ok=True)
        return out

    def save_part(self, index, pages, result, config):
        check = getattr(config.translator, 'raise_if_failed', None)
        if check:
            check()
        self.check()
        paths = {}
        for kind in ('mono', 'dual'):
            if getattr(config, 'no_' + kind):
                continue
            path = getattr(result, kind + '_pdf_path')
            if not path or Path(path).resolve().parent != self.part_output(index).resolve():
                raise ValueError('翻译分段未生成有效输出。')
            with Path(path).open('rb+') as stream:
                os.fsync(stream.fileno())
            paths[kind] = {'name': Path(path).name, 'sha256': file_hash(path)}
        context = config.shared_context_cross_split_part
        titles = {key: asdict(value) if (value := getattr(context, key)) is not None else None for key in ('first_paragraph', 'recent_title_paragraph')}
        atomic_json(self._marker(index), {'files': paths, 'pages': pages, 'titles': titles, 'ocr': context.auto_enabled_ocr_workaround})
        if not self.auto_save:
            with self.journal.lock:
                self.journal.pending.clear()  # Completed part now holds this work.

    def restore_part(self, index, pages, config):
        from babeldoc.format.pdf.translation_config import TranslateResult, TitleContextSnapshot
        marker = self._marker(index)
        if not marker.exists():
            return None
        content = json.loads(marker.read_text(encoding='utf-8'))
        paths = {}
        for kind in ('mono', 'dual'):
            if getattr(config, 'no_' + kind):
                continue
            saved = content['files'][kind]
            path = (self.part_output(index)/saved['name']).resolve()
            if path.parent != self.part_output(index).resolve() or content['pages'] != pages or not path.is_file() or file_hash(path) != saved['sha256']:
                raise ValueError('已保存的分段 PDF 不匹配或已损坏。')
            paths[kind] = path
        context = config.shared_context_cross_split_part
        for key, title in content['titles'].items():
            if key in ('first_paragraph', 'recent_title_paragraph'):
                setattr(context, key, TitleContextSnapshot(**title) if title else None)
        context.auto_enabled_ocr_workaround = content['ocr']
        return TranslateResult(paths.get('mono'), paths.get('dual'))
