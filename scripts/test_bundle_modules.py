"""Packaging regression checks independent of runtime caches and credentials."""
from pathlib import Path
import tempfile

from bundle_modules import assert_module_inventory, collect_source_modules, vendor_modules


def main():
    with tempfile.TemporaryDirectory() as temporary:
        package = Path(temporary) / "sample_package"
        adapter = package / "translator" / "translator_impl"
        adapter.mkdir(parents=True)
        # The package initializer must never execute during discovery.
        (package / "__init__.py").write_text("raise RuntimeError('must not import at build time')\n", encoding="utf-8")
        (adapter / "deepl.py").write_text("class DeepLTranslator: pass\n", encoding="utf-8")
        (adapter / "google.py").write_text("class GoogleTranslator: pass\n", encoding="utf-8")
        (package / "gui.py").write_text("raise RuntimeError('not bundled')\n", encoding="utf-8")
        modules = collect_source_modules(package, "sample_package", ("sample_package.gui",))
        assert modules["sample_package.translator"] is None
        assert modules["sample_package.translator.translator_impl"] is None
        assert "sample_package.gui" not in modules
        assert "sample_package.translator.translator_impl.deepl" in modules
        assert "sample_package.translator.translator_impl.google" in modules
        complete = [(name, source, "PYMODULE") for name, source in modules.items() if source]
        assert assert_module_inventory(modules, complete)["ok"]
        incomplete = [entry for entry in complete if not entry[0].endswith(".deepl")]
        try:
            assert_module_inventory(modules, incomplete)
            raise AssertionError("Missing dynamic adapter was not rejected")
        except RuntimeError as exc:
            assert "sample_package.translator.translator_impl.deepl" in str(exc)
        try:
            vendor_modules(Path(temporary))
            raise AssertionError("Missing vendor directory was not rejected")
        except FileNotFoundError:
            pass

    root = Path(__file__).resolve().parent.parent
    modules = vendor_modules(root / "vendor")
    engines = [name for name, source in modules.items() if source and ".translator_impl." in name]
    assert len(engines) == 15, engines
    assert "pdf2zh_next.translator.translator_impl.indextranslate" in engines
    assert not any(name.endswith((".gui", ".clitranslator", ".claudecode")) for name in modules)
    print("PASS: no import side effects, namespace discovery, missing-adapter build failure, 15 real engine implementation sources.")


if __name__ == "__main__":
    main()
