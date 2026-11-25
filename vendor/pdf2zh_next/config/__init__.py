"""Lazy public configuration exports, without eagerly loading the CLI manager."""
from importlib import import_module

_ENGINE_SETTINGS = (
    "AnythingLLMSettings", "AzureOpenAISettings", "AzureSettings", "BingSettings",
    "ClaudeCodeSettings", "DeepLSettings", "DeepSeekSettings", "DifySettings",
    "GeminiSettings", "GoogleSettings", "GrokSettings", "GroqSettings",
    "IndexTranslateSettings", "ModelScopeSettings", "OllamaSettings", "OpenAISettings",
    "QwenMtSettings", "SiliconFlowSettings", "TencentSettings", "XinferenceSettings",
    "ZhipuSettings", "TRANSLATION_ENGINE_METADATA",
)
_EXPORTS = {name: "pdf2zh_next.config.translate_engine_model" for name in _ENGINE_SETTINGS}
_EXPORTS.update({name: "pdf2zh_next.config.model" for name in (
    "SettingsModel", "BasicSettings", "TranslationSettings", "PDFSettings", "WatermarkOutputMode",
)})
_EXPORTS["ConfigManager"] = "pdf2zh_next.config.main"

__all__ = [
    "ConfigManager", "SettingsModel", "BasicSettings", "TranslationSettings", "PDFSettings",
    "WatermarkOutputMode", "BingSettings", "GoogleSettings", "OpenAISettings", "DeepLSettings",
    "OllamaSettings", "XinferenceSettings", "AzureOpenAISettings", "ModelScopeSettings",
    "ZhipuSettings", "SiliconFlowSettings", "TencentSettings", "GeminiSettings", "AzureSettings",
    "AnythingLLMSettings", "DifySettings", "GrokSettings", "GroqSettings", "QwenMtSettings",
    "DeepSeekSettings", "TRANSLATION_ENGINE_METADATA", "ClaudeCodeSettings", "IndexTranslateSettings",
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
