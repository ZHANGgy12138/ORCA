"""Public PDFMathTranslate API, loaded only when a caller requests it.

ORCA reads the engine schema before translation. Importing that schema must not
also import BabelDOC's PDF pipeline, OpenCV, and scientific-computing libraries.
The public objects keep their original defining modules for multiprocessing and
serialization compatibility.
"""
from importlib import import_module

__version__ = "2.9.0"
__author__ = "Byaidu, awwaawwa"
__license__ = "AGPL-3.0"
__maintainer__ = "awwaawwa"
__email__ = "aw@funstory.ai"

_ENGINE_SETTINGS = (
    "AnythingLLMSettings", "AzureOpenAISettings", "AzureSettings", "BingSettings",
    "ClaudeCodeSettings", "DeepLSettings", "DeepSeekSettings", "DifySettings",
    "GeminiSettings", "GoogleSettings", "GrokSettings", "GroqSettings",
    "IndexTranslateSettings", "ModelScopeSettings", "OllamaSettings", "OpenAISettings",
    "QwenMtSettings", "SiliconFlowSettings", "TencentSettings", "XinferenceSettings",
    "ZhipuSettings",
)
_EXPORTS = {name: "pdf2zh_next.config.translate_engine_model" for name in _ENGINE_SETTINGS}
_EXPORTS.update({name: "pdf2zh_next.config.model" for name in (
    "SettingsModel", "BasicSettings", "PDFSettings", "TranslationSettings", "WatermarkOutputMode",
)})
_EXPORTS["ConfigManager"] = "pdf2zh_next.config.main"
_EXPORTS.update({name: "pdf2zh_next.high_level" for name in (
    "create_babeldoc_config", "do_translate_async_stream", "do_translate_file", "do_translate_file_async",
)})

__all__ = [
    "SettingsModel", "BasicSettings", "OpenAISettings", "BingSettings",
    "GoogleSettings", "DeepLSettings", "DeepSeekSettings", "OllamaSettings",
    "XinferenceSettings", "AzureOpenAISettings", "ModelScopeSettings", "ZhipuSettings",
    "SiliconFlowSettings", "TencentSettings", "GeminiSettings", "AzureSettings",
    "AnythingLLMSettings", "DifySettings", "GrokSettings", "GroqSettings", "QwenMtSettings",
    "PDFSettings", "TranslationSettings", "WatermarkOutputMode", "do_translate_file_async",
    "do_translate_file", "do_translate_async_stream", "create_babeldoc_config", "ConfigManager",
    "ClaudeCodeSettings", "IndexTranslateSettings",
]


def __getattr__(name):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
