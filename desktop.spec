# PyInstaller build specification; AGPL-3.0 desktop modifications 2026-11-16.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata, collect_delvewheel_libs_directory

root = Path(SPECPATH)
sys.path.insert(0, str(root / 'scripts'))
from bundle_modules import EXCLUDED_MODULES, assert_module_inventory, vendor_modules, write_inventory
vendored_modules = vendor_modules(root / 'vendor')
datas = [(str(root / 'ui'), 'ui'), (str(root / 'resources'), 'resources'), (str(root / 'app.ico'), '.')]
binaries = []
# Keep wheel-specific DLL directories: spawned workers import hyperscan before numpy.
for package in ('hyperscan', 'onnx', 'ml_dtypes', 'rapidfuzz', 'Levenshtein'):
    datas, binaries = collect_delvewheel_libs_directory(package, datas=datas, binaries=binaries)
for package in ('pdf2zh_next', 'babeldoc', 'webview', 'tiktoken_ext'):
    datas += collect_data_files(package)
datas += copy_metadata('pdf2zh-next', recursive=True)
datas += copy_metadata('pywebview', recursive=True)
hidden = list(vendored_modules)
hidden += collect_submodules('babeldoc', on_error='raise')
hidden += collect_submodules('bitstring', on_error='raise')
hidden += collect_submodules('tibs', on_error='raise')
hidden += ['webview.platforms.edgechromium', 'webview.platforms.winforms', 'tiktoken_ext.openai_public', 'desktop_compat', 'engine_registry', 'preferences', 'window_chrome']
a = Analysis([str(root / 'app.py')], pathex=[str(root / 'vendor'), str(root)], binaries=binaries, datas=datas, hiddenimports=hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[str(root / 'runtime_hook.py')],
    excludes=[*EXCLUDED_MODULES, 'gradio', 'gradio_client', 'gradio_pdf', 'torch', 'tensorflow', 'matplotlib', 'IPython', 'pytest', 'webview.platforms.cef', 'webview.platforms.qt', 'webview.platforms.gtk', 'webview.platforms.cocoa'])
# Missing dynamic adapters must stop the build instead of becoming a runtime
# "No module named" error in the user's engine settings panel.
inventory = assert_module_inventory(vendored_modules, a.pure, a.binaries)
write_inventory(root / 'build' / 'bundled-module-manifest.json', inventory)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='ORCA', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False, icon=str(root / 'app.ico'), version=str(root / 'version_info.txt'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='ORCA-0.9.1')
