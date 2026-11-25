"""Discover ORCA's vendored modules without importing application code.

PyInstaller's import-based collector can otherwise silently return no dynamic
adapters when an import tries to initialize a cache in an inaccessible folder.
"""
import json
from pathlib import Path


EXCLUDED_MODULES = (
    "pdf2zh_next.gui",
    "pdf2zh_next.main",
    "pdf2zh_next.http_api",
    "pdf2zh_next.i18n",
    "pdf2zh_next.translator.translator_impl.clitranslator",
    "pdf2zh_next.translator.translator_impl.claudecode",
)
# These are implementation modules, not the 23 UI choices. Several settings
# types intentionally transform to OpenAISettings before provider resolution.
REQUIRED_ADAPTERS = (
    "anythingllm", "azure", "azureopenai", "bing", "deepl", "dify",
    "google", "indextranslate", "ollama", "openai", "qwenmt", "siliconflow",
    "siliconflowfree", "tencentmechinetranslation", "xinference",
)


def collect_source_modules(package_root, package_name, excluded=()):
    """Map import names to source paths; namespace parents have a None path."""
    package_root = Path(package_root).resolve(strict=True)
    if not package_root.is_dir() or not all(part.isidentifier() for part in package_name.split(".")):
        raise ValueError("A package directory and valid package name are required")

    def allowed(name):
        return not any(name == item or name.startswith(item + ".") for item in excluded)

    modules = {}
    for source in sorted(package_root.rglob("*.py")):
        parts = source.relative_to(package_root).with_suffix("").parts
        if "__pycache__" in parts or not all(part.isidentifier() for part in parts):
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        name = ".".join((package_name, *parts))
        if not allowed(name):
            continue
        modules[name] = str(source)
        parent = name
        while parent != package_name:
            parent = parent.rsplit(".", 1)[0]
            modules.setdefault(parent, None)
    if not modules:
        raise RuntimeError("No Python modules found in " + str(package_root))
    return dict(sorted(modules.items()))


def vendor_modules(vendor_root):
    modules = collect_source_modules(Path(vendor_root) / "pdf2zh_next", "pdf2zh_next", EXCLUDED_MODULES)
    required = {"pdf2zh_next.translator.translator_impl." + name for name in REQUIRED_ADAPTERS}
    missing = sorted(required - modules.keys())
    if missing:
        raise RuntimeError("Vendored engine implementations missing: " + ", ".join(missing))
    return modules


def assert_module_inventory(expected, pure, binaries=()):
    """Fail a build if Analysis omitted a required Python implementation.

    Namespace package entries may be represented separately by PyInstaller;
    the actual .py modules must all be in the frozen module inventory.
    """
    found = {entry[0] for entry in (*pure, *binaries)}
    required = {name for name, source in expected.items() if source is not None}
    missing = sorted(required - found)
    if missing:
        raise RuntimeError("Frozen engine module inventory incomplete: " + ", ".join(missing))
    return {
        "ok": True,
        "expected_modules": sorted(required),
        "namespace_packages": sorted(name for name, source in expected.items() if source is None),
        "engine_modules": sorted(name for name in required if ".translator_impl." in name),
        "missing_modules": missing,
    }


def write_inventory(path, inventory):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    modules = vendor_modules(root / "vendor")
    engines = [name for name, source in modules.items() if source and ".translator_impl." in name]
    print(f"Vendored source check passed: {len(modules)} module names, {len(engines)} engine implementations.")
