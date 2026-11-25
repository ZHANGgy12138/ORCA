"""ORCA adapters for the bundled PDFMathTranslate service catalog.

Catalog schemas contain no credentials. Settings are validated using the upstream
models, then passed unchanged to the upstream translation implementations.
"""
from functools import lru_cache
import logging
import multiprocessing
import re
import typing
from urllib.parse import parse_qsl, unquote, urlsplit

EXCLUDED = {"CLITranslator", "ClaudeCode"}  # Require separately installed executables.
LABELS = {
    "Bing": "Bing · 免费翻译", "Google": "Google · 免费翻译",
    "IndexTranslate": "Index-Translate · B站免费翻译",
    "OpenAI": "OpenAI · GPT", "OpenAICompatible": "自定义 · OpenAI 兼容接口",
    "DeepL": "DeepL", "DeepSeek": "DeepSeek", "Ollama": "Ollama · 本地模型",
    "Xinference": "Xinference · 自托管模型", "Gemini": "Google Gemini",
    "AliyunDashScope": "阿里云百炼", "QwenMt": "通义千问翻译",
    "ModelScope": "魔搭 ModelScope", "Zhipu": "智谱 GLM", "SiliconFlow": "硅基流动",
    "SiliconFlowFree": "硅基流动 · 公共免费服务", "TencentMechineTranslation": "腾讯云机器翻译",
    "Azure": "Azure 翻译", "AzureOpenAI": "Azure OpenAI", "Grok": "xAI Grok",
    "Groq": "Groq", "AnythingLLM": "AnythingLLM", "Dify": "Dify",
}
FREE = {"bing", "google", "siliconflowfree", "indextranslate"}
LOCAL = {"ollama", "xinference", "anythingllm", "dify"}


@lru_cache(maxsize=1)
def metadata():
    from pdf2zh_next.config.translate_engine_model import TRANSLATION_ENGINE_METADATA
    return {m.translate_engine_type.lower(): m for m in TRANSLATION_ENGINE_METADATA if m.translate_engine_type not in EXCLUDED}


def engine_id(value):
    key = str(value or "bing").lower()
    key = {"custom": "openaicompatible", "openai-compatible": "openaicompatible", "tencent": "tencentmechinetranslation", "qwen": "aliyundashscope"}.get(key, key)
    if key not in metadata():
        raise ValueError("请选择受支持的翻译服务。")
    return key


def is_secret(name):
    from pdf2zh_next.config.translate_engine_model import GUI_PASSWORD_FIELDS
    return name in GUI_PASSWORD_FIELDS or bool(re.search(r"api_?key|auth_?key|secret|password|access_?token", name, re.I))


def field_label(name):
    if name == "azure_region": return "资源区域"
    if "secret_id" in name: return "Secret ID"
    if "secret_key" in name: return "Secret Key"
    if is_secret(name): return "API 密钥"
    for suffix, label in [
        ("send_reasoning_effort", "发送推理强度参数"), ("send_temperature", "发送温度参数"),
        ("send_temprature", "发送温度参数"), ("send_enable_thinking_param", "发送思考模式参数"),
        ("enable_json_mode", "启用 JSON 输出"), ("enable_thinking", "启用思考模式"),
        ("thinking_mode", "思考模式（enabled / disabled）"), ("reasoning_effort", "推理强度"),
        ("api_version", "API 版本"), ("base_url", "接口地址"), ("endpoint", "接口地址"),
        ("host", "服务地址"), ("url", "服务地址"), ("model", "模型名称"),
        ("timeout", "请求超时（秒）"), ("temperature", "温度"),
        ("num_predict", "最大生成词元数"), ("ali_domains", "专业领域说明"),
    ]:
        if name.endswith(suffix): return label
    return name.replace("_", " ")


def get_catalog():
    records = []
    order = ["bing", "indextranslate", "google", "openai", "openaicompatible", "deepseek", "gemini", "aliyundashscope", "qwenmt", "siliconflow", "modelscope", "zhipu", "deepl", "ollama", "xinference"]
    for key, item in metadata().items():
        fields = []
        for name, field in item.setting_model_type.model_fields.items():
            if name in ("translate_engine_type", "support_llm"):
                continue
            secret = is_secret(name)
            is_bool = field.annotation is bool or bool in typing.get_args(field.annotation)
            default = field.default if isinstance(field.default, (str, int, float, bool)) else None
            if key == "openai" and name == "openai_base_url": default = "https://api.openai.com/v1"
            fields.append({"name": name, "label": field_label(name), "type": "password" if secret else "checkbox" if is_bool else "text",
                           "default": None if secret else default, "placeholder": "输入此服务的密钥" if secret else ("可填写自定义模型名称" if name.endswith("model") else ""),
                           "advanced": not (secret or name.endswith(("model", "base_url", "host", "url", "endpoint", "api_version")))})
        category = "免费服务" if key in FREE else "本地与自托管" if key in LOCAL else "模型与云服务"
        description = "无需密钥；公共服务可能限流。" if key in FREE else "连接自己的模型服务；请先启动相应服务。" if key in LOCAL else "使用自己的服务账号、密钥与可用模型。"
        if key == "openaicompatible": description = "填写兼容 OpenAI 的接口地址和自定义模型名称。"
        if key == "indextranslate": description = "使用 B站官方 Index-Translate-35B-A3B 公共接口，无需密钥；所选文字发送至该服务，公共接口可能限流。"
        records.append({"id": key, "label": LABELS.get(item.translate_engine_type, item.translate_engine_type), "description": description, "category": category, "fields": fields})
    return sorted(records, key=lambda item: (order.index(item["id"]) if item["id"] in order else 99, item["label"]))


def safe_settings(key, values):
    """Whitelist nonsecret model settings for disk persistence."""
    model = metadata()[engine_id(key)].setting_model_type
    result = {}
    for name, value in (values or {}).items():
        if name not in model.model_fields or name in ("translate_engine_type", "support_llm") or is_secret(name):
            continue
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            continue
        if isinstance(value, str):
            if re.search(r"\bsk-[A-Za-z0-9_-]{12,}", value):
                continue
            if name.endswith(("url", "host", "endpoint")) and value:
                try:
                    parsed = urlsplit(value)
                except ValueError:
                    continue
                # Do not persist credentials embedded in URLs or query strings.
                if parsed.username or parsed.password or parsed.query or parsed.fragment:
                    continue
        result[name] = value
    return result


def secret_values(options):
    options = options if isinstance(options, dict) else {}
    values = options.get("engine_settings", {})
    values = values if isinstance(values, dict) else {}
    result = [str(v) for k, v in values.items() if is_secret(k) and v]
    if options.get("api_key"):
        result.append(str(options["api_key"]))
    for name, value in values.items():
        if isinstance(value, str) and name.endswith(("url", "host", "endpoint")):
            try:
                parsed = urlsplit(value)
            except ValueError:
                result.append(value)
                continue
            if parsed.username or parsed.password or parsed.query:
                result.append(value)
                result.extend(unquote(item) for item in (parsed.username, parsed.password) if item)
                result.extend(item for _, item in parse_qsl(parsed.query) if item)
    return result


def redact(message, secrets=()):
    text = str(message)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[密钥已隐藏]")
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[密钥已隐藏]", text)
    return text[:1200]


def service_error(error, secrets=()):
    """Explain local packaging failures separately from remote service errors."""
    # Upstream retry decorators wrap the useful HTTP/SDK error in RetryError.
    # Classify the final attempt instead of showing a Python Future address.
    for _ in range(3):
        attempt = getattr(error, "last_attempt", None)
        if type(error).__name__ != "RetryError" or attempt is None:
            break
        cause = attempt.exception()
        if cause is None or cause is error:
            break
        error = cause
    if isinstance(error, (ModuleNotFoundError, ImportError)):
        return "翻译组件加载失败，请完整解压最新版 ORCA 后重试；仅更换密钥无法解决此问题。"
    status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
    kind = type(error).__name__.lower()
    if status in (401, 403) or kind in ("authenticationerror", "permissiondeniederror", "authorizationexception"):
        return "服务认证或访问权限检查失败，请核对该服务的 API 密钥、接口地址及模型权限。"
    if status == 456 or kind == "quotaexceededexception":
        return "此服务的可用额度已用完，请检查服务账号的额度。"
    if status == 429 or kind in ("ratelimiterror", "toomanyrequestsexception"):
        return "此服务暂时限制了请求，或账号额度不足，请稍后重试并检查服务额度。"
    if "timeout" in kind:
        return "连接翻译服务超时，请检查网络和服务地址后重试。"
    if kind in ("connectionerror", "apiconnectionerror", "connecterror"):
        return "无法连接翻译服务，请检查网络、接口地址或本地服务是否已启动。"
    return redact(error, secrets)


class SecretFilter(logging.Filter):
    def __init__(self, values):
        super().__init__()
        self.values = tuple(values)

    def filter(self, record):
        record.msg = redact(record.getMessage(), self.values)
        record.args = ()
        record.exc_info = None  # Tracebacks can include unredacted provider request bodies.
        record.exc_text = None
        return True


def build_engine(options):
    key = engine_id(options.get("engine"))
    model = metadata()[key].setting_model_type
    values = dict(options.get("engine_settings") or {})
    if key == "openai":
        if options.get("api_key"): values.setdefault("openai_api_key", options["api_key"])
        if options.get("model"): values.setdefault("openai_model", options["model"])
    allowed = set(model.model_fields) - {"translate_engine_type", "support_llm"}
    values = {k: (v.strip() if isinstance(v, str) else v) for k, v in values.items() if k in allowed}
    for name in list(values):
        if values[name] == "":
            del values[name]
    try:
        result = model(**values)
        result.validate_settings()
    except Exception as exc:
        message = redact(exc, secret_values(options))
        if "required" in message.lower():
            raise ValueError("请补全此服务的密钥和必填设置。") from None
        raise ValueError("引擎设置无效：" + message) from None
    return result


def _test_worker(options, pipe):
    # No request bodies, credentials or provider tracebacks should enter logs.
    logging.disable(logging.CRITICAL)
    try:
        import truststore
        truststore.inject_into_ssl()
        from desktop_compat import install
        install()
        from pdf2zh_next.config.model import SettingsModel, TranslationSettings
        from pdf2zh_next.translator.utils import get_translator
        settings = SettingsModel(translate_engine_settings=build_engine(options), translation=TranslationSettings(lang_in="en", lang_out="zh", no_auto_extract_glossary=True, qps=1))
        settings.validate_settings()
        # Upstream get_translator already sends one short Hello health request.
        get_translator(settings)
        pipe.send({"ok": True, "message": "连接成功，服务已返回翻译结果。"})
    except Exception as exc:
        pipe.send({"ok": False, "error": service_error(exc, secret_values(options))})
    finally:
        pipe.close()


def test_connection(options, timeout=30):
    build_engine(options)  # Catch incomplete settings before creating a process.
    receive, send = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(target=_test_worker, args=(options, send), daemon=True)
    process.start()
    send.close()
    try:
        try:
            ready = receive.poll(timeout)
        except (EOFError, OSError):
            ready = True
        if ready:
            try:
                return receive.recv()
            except (EOFError, OSError):
                return {"ok": False, "error": "连接测试进程退出，请检查服务地址和配置。"}
        return {"ok": False, "error": "连接测试超时，请检查网络或本地模型服务是否已启动。"}
    finally:
        receive.close()
        process.join(0.2)
        if process.is_alive():
            process.terminate()
            process.join(2)
        process.close()
