"""Check PDF part reuse, context continuity, source identity, queue restore and privacy."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'vendor')]
os.environ.setdefault('PDFMT_DATA_DIR', str(ROOT / 'test-data' / 'split-checkpoint-test'))
os.environ.setdefault('PDFMT_CACHE_DIR', str(Path(os.environ['PDFMT_DATA_DIR']) / 'cache' / 'babeldoc'))


def test_parts(root):
    import pymupdf
    from babeldoc.format.pdf.translation_config import TranslateResult, TitleContextSnapshot, SharedContextCrossSplitPart
    from resume_store import FileCheckpoint
    checkpoint = FileCheckpoint(root, True)
    checkpoint.journal = SimpleNamespace(failed=False)
    config = SimpleNamespace(no_mono=False, no_dual=False, translator=None, shared_context_cross_split_part=SharedContextCrossSplitPart())
    config.shared_context_cross_split_part.recent_title_paragraph = TitleContextSnapshot('abc', 'Methodology', 'title')
    config.shared_context_cross_split_part.auto_enabled_ocr_workaround = True
    mono, dual = (checkpoint.part_output(0)/name for name in ('chapter.zh.mono.pdf','chapter.zh.dual.pdf'))
    for path, width in ((mono, 612), (dual, 1224)):
        with pymupdf.open() as pdf:
            pdf.new_page(width=width,height=792).insert_text((30, 30), 'Original text; translated column.')
            pdf.save(path)
    checkpoint.save_part(0, 1, TranslateResult(mono, dual), config)
    config.shared_context_cross_split_part = SharedContextCrossSplitPart()
    replay = FileCheckpoint(root, True).restore_part(0, 1, config)
    assert replay.mono_pdf_path == mono and replay.dual_pdf_path == dual
    with pymupdf.open(replay.dual_pdf_path) as pdf:
        assert pdf[0].rect.width == 1224
    assert config.shared_context_cross_split_part.recent_title_paragraph.unicode == 'Methodology'
    assert config.shared_context_cross_split_part.auto_enabled_ocr_workaround
    mono.write_bytes(b'invalid')
    try:
        checkpoint.restore_part(0, 1, config)
    except ValueError:
        pass
    else:
        raise AssertionError('Corrupt checkpoint accepted')
    assert not checkpoint.restore_part(1, 1, config)


def test_queue(root):
    from desktop_api import DesktopApi
    from scripts.test_batch import make_pdf
    from resume_store import file_hash
    files = [root / name for name in ('first.pdf','second.pdf')]
    for file in files:
        make_pdf(file, 3)
    start = DesktopApi(ROOT, ROOT, root / 'profile', 'test')
    start.prepare_assets = lambda: None
    start.select_files(list(map(str, files)))
    gate = threading.Event()
    called = []

    async def first_run(settings, path):
        called.append(Path(path).name)
        assert settings.pdf.pages == '1,3'
        if Path(path) == files[1]:
            gate.set()
            await asyncio.Future()
        out = Path(settings.translation.output) / 'translated.pdf'
        shutil.copy2(path, out)
        return {'dual': str(out)}

    start._translate = first_run
    secret = 'test-api-secret-not-for-persistence'
    url = 'http://localhost:82/custom/v1'
    options = {'engine':'openaicompatible','output':'dual','page_mode':'custom','pages':'1,3','auto_save':False,
               'engine_settings': {'openai_compatible_base_url':url, 'openai_compatible_model':'t', 'openai_compatible_api_key':secret}}
    assert start.start_translation(options)['ok'] and gate.wait(20)
    assert not start.recovery.list()
    assert start.pause_and_save()['ok']
    start.thread.join(12)
    assert start.get_state()['status'] == 'paused',start.get_state()
    pending = start.list_recoveries()['recoveries']
    assert len(pending) == 1 and pending[0]['saved_pages'] == 2 and pending[0]['total_pages'] == 4
    task_id = pending[0]['id']
    record = start.recovery.load(task_id)
    assert secret not in json.dumps(record)
    assert all(file['sha256'] for file in record['files'])
    assert record['options']['engine_settings']['openai_compatible_model'] == 't'
    assert called == ['first.pdf','second.pdf']
    start.close()
    resumed = DesktopApi(ROOT, ROOT, root / 'profile', 'test')
    resumed.prepare_assets = lambda: None
    other = {**options,'engine_settings':{**options['engine_settings'],'openai_compatible_base_url':'http://localhost:9999/new'}}
    assert resumed.resume_saved(task_id,other).get('field') == 'engine'
    moved = root / 'moved.pdf'
    files[1].rename(moved)
    timing = resumed.resume_saved(task_id,options)
    assert not timing['ok'] and timing['field']=='source'
    pick = SimpleNamespace(create_file_dialog=lambda *args,**kwargs:[str(files[0])])
    resumed.window=pick
    # A different source with equal dimensions must still fail identity checks.
    with open(files[0], 'ab') as stream:
        stream.write(b'\n% different')
    assert not resumed.relocate_saved(task_id,timing['file_id'])['ok']
    files[0].write_bytes(Path(record['files'][0]['results']['dual']).read_bytes())
    resumed.window = SimpleNamespace(create_file_dialog=lambda *args,**kwargs:[str(moved)])
    assert resumed.relocate_saved(task_id,timing['file_id'])['ok']
    calls = []
    async def finish(settings, path):
        calls.append(Path(path))
        assert settings.pdf.pages == '1,3'
        dest = Path(settings.translation.output) / 'translated.pdf'
        shutil.copy2(path,dest)
        return {'dual':str(dest)}
    resumed._translate = finish
    assert resumed.resume_saved(task_id,options)['ok']
    resumed.thread.join(10)
    assert resumed.get_state()['status'] == 'done' and calls==[moved]
    assert not resumed.list_recoveries()['recoveries']
    assert Path(record['files'][0]['results']['dual']).exists()
    resumed.close()


def test_secrets(root):
    from resume_store import ResumeStore
    opts={'engine':'openaicompatible','lang_in':'en','lang_out':'zh','page_mode':'all','output':'both',
          '_output_dir':str(root),'_snapshot':{'engine_label':'compat'},'engine_settings':{
              'openai_compatible_api_key':'test-sensitive-key','openai_compatible_base_url':'https://example.invalid/v1?token=private-query','unknown_key':'private-unknown'}}
    record=ResumeStore(root).create('0'*32,opts,[])
    serialized=json.dumps(record)
    assert not any(s in serialized for s in ('test-sensitive-key','private-query','private-unknown'))
    assert 'openai_compatible_base_url' in record['omitted']


if __name__ == '__main__':
    base=Path(os.environ['PDFMT_DATA_DIR']);base.mkdir(exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(dir=base) as tmp:
        folder=Path(tmp);test_parts(folder/'parts');test_queue(folder);test_secrets(folder)
    print('PASS: native mono/dual part replay, title and OCR context, corruption refusal, manual queue pause, restart/skip completed file, original page ranges, relocation with SHA check, secret/URL filtering and endpoint isolation.')
