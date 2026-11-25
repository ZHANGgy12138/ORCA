"""ORCA's adapter for the official public Bilibili Index-Translate API.

Protocol and prompt follow bilibili/Index-Translate/inference/llm/call_api.py.
This adapter translates paragraphs, not BabelDOC's JSON-batch LLM prompts.
"""
from collections import Counter
import re
import threading

import requests
from pdf2zh_next.translator.base_translator import BaseTranslator


MODEL = "Index-Translate-35B-A3B"
API_URL = "https://index-translate.bilibili.com/v1/chat/completions"
TOKEN = re.compile(r"\{\s*v\d+\s*\}|<\s*style\s+id\s*=\s*['\"]\d+['\"]\s*>|<\s*/\s*style\s*>", re.IGNORECASE)
LANGUAGES = {
    "auto": "", "en": "英语", "zh": "中文", "zh-cn": "简体中文", "zh-hans": "简体中文",
    "zh-tw": "繁体中文", "zh-hant": "繁体中文", "ja": "日语", "ko": "韩语",
    "de": "德语", "fr": "法语", "es": "西班牙语", "ru": "俄语", "it": "意大利语", "pt": "葡萄牙语",
}
SOFT_CHUNK = 1800
MAX_PROTECTED_CHUNK = 6000


class _NoAuth(requests.auth.AuthBase):
    def __call__(self, request):
        return request


def _protected_ranges(text):
    """Do not split formula markers or balanced rich-text spans across calls."""
    ranges, stack = [], []
    for match in TOKEN.finditer(text):
        value = match.group()
        if value.startswith("{"):
            if not stack:
                ranges.append((match.start(), match.end()))
        elif re.match(r"<\s*/", value):
            if not stack:
                raise ValueError("文档中的样式标记不完整，无法安全翻译此段。")
            start = stack.pop()
            if not stack:
                ranges.append((start, match.end()))
        else:
            stack.append(match.start())
    if stack:
        raise ValueError("文档中的样式标记不完整，无法安全翻译此段。")
    return ranges


def split_preserving_markers(text):
    """Return exact source slices; a protected span may exceed the soft size."""
    protected = _protected_ranges(text)

    def safe(position):
        return not any(start < position < end for start, end in protected)

    chunks, start = [], 0
    while len(text) - start > SOFT_CHUNK:
        limit = start + SOFT_CHUNK
        candidates = [start + match.end() for match in re.finditer(r"[.!?;]\s+|[。！？；]|\n+|\s+", text[start:limit]) if safe(start + match.end())]
        end = candidates[-1] if candidates else limit
        for protected_start, protected_end in protected:
            if protected_start < end < protected_end:
                end = protected_start if protected_start > start else protected_end
                break
        if end <= start:
            raise ValueError("文档标记无法安全分段，请缩小翻译范围。")
        if end - start > MAX_PROTECTED_CHUNK:
            raise ValueError("此段连续格式内容过长，请缩小选区后重试。")
        chunks.append(text[start:end])
        start = end
    if start < len(text):
        chunks.append(text[start:])
    return chunks


def validate_markers(source, translated):
    before = TOKEN.findall(source)
    after = TOKEN.findall(translated)
    if Counter(before) != Counter(after) or before != after:
        raise ValueError("Index-Translate 未完整保留公式或样式标记，请重试或更换引擎。")
    _protected_ranges(translated)


class IndexTranslateTranslator(BaseTranslator):
    name = "indextranslate"
    ENDPOINT = API_URL
    MODEL = MODEL
    REQUEST_TIMEOUT = (5, 20)
    MAX_TOKENS = 4096

    def __init__(self, settings, rate_limiter):
        super().__init__(settings, rate_limiter)
        self.model = self.MODEL
        self.session = requests.Session()
        # Preserve proxy settings, but never pick up an unrelated .netrc key
        # for this public endpoint. Redirects are not needed by the API.
        self.session.auth = _NoAuth()
        self.failed_count = 0
        self._failure_lock = threading.Lock()
        self.add_cache_impact_parameters("model", self.model)
        self.add_cache_impact_parameters("prompt", "index-paragraph-v1-markers")
        self.add_cache_impact_parameters("endpoint", self.ENDPOINT)
        self.add_cache_impact_parameters("chunk_chars", SOFT_CHUNK)

    def prompt(self, text):
        source = LANGUAGES.get(self.lang_in, self.lang_in)
        target = LANGUAGES.get(self.lang_out, self.lang_out)
        instruction = f"请将以下{source}文本翻译为{target}，直接输出翻译结果，不要进行任何解释。"
        if TOKEN.search(text):
            instruction += "\n〖硬性要求〗所有形如 {v1} 的公式占位符以及 <style id='1'> 和 </style> 样式标记必须原样保留，不得添加、删除、修改或改变标记先后顺序；仅翻译标记之外及样式标记包裹的自然语言文字。"
        return [{"role": "user", "content": instruction + "\n\n" + text}]

    def _translate_chunk(self, text):
        content = self._completion(self.prompt(text))
        validate_markers(text, content)
        return content

    def translate_image(self, data_url):
        target = LANGUAGES.get(self.lang_out, self.lang_out)
        prompt = (f"请识别图中清晰可见的文字，并将图例、坐标轴和标签翻译为{target}。"
                  "每行输出一组原文和译文，保留数字、单位和符号。"
                  "不要推断曲线趋势或补写模糊的文字。如果没有可辨认文字，仅输出 [NO_TEXT]。")
        return self._completion([{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}])

    def _completion(self, messages):
        response = self.session.post(
            self.ENDPOINT,
            headers={"Content-Type": "application/json", "User-Agent": "Index-Translate-Client/1.0"},
            json={"model": self.model, "messages": messages, "temperature": 0.0,
                  "max_tokens": self.MAX_TOKENS, "stream": False,
                  "chat_template_kwargs": {"enable_thinking": False}},
            timeout=self.REQUEST_TIMEOUT,
            allow_redirects=False,
        )
        if response.status_code == 429:
            raise ValueError("Index-Translate 公共接口暂时限流，请稍后再试或更换引擎。")
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Index-Translate 公共接口暂时无法返回译文，请稍后重试。")
        try:
            result = response.json()
            choice = result["choices"][0]
            reason = choice.get("finish_reason")
            content = choice["message"].get("content")
        except (ValueError, TypeError, KeyError, IndexError):
            raise ValueError("Index-Translate 返回了无法读取的翻译结果，请重试。") from None
        if reason != "stop":
            raise ValueError("Index-Translate 未完整生成译文，请缩小选区或稍后重试。")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Index-Translate 未返回译文，请重试或更换引擎。")
        # A well-formed reasoning prefix is discarded; reasoning-only or an
        # unterminated reasoning block must never be shown as a translation.
        content = content.strip()
        if content.startswith("<think>"):
            if "</think>" not in content:
                raise ValueError("Index-Translate 返回了思考内容而非译文，请重试。")
            content = content.split("</think>", 1)[1].strip()
        if not content or "<think>" in content or "</think>" in content:
            raise ValueError("Index-Translate 未返回可用的译文，请重试。")
        return content

    def do_translate(self, text, rate_limit_params=None):
        if not isinstance(text, str):
            raise ValueError("需要翻译的内容必须是文字。")
        if not text.strip():
            return text
        try:
            results = []
            for index, chunk in enumerate(split_preserving_markers(text)):
                if index:
                    self.rate_limiter.wait(rate_limit_params)
                if not chunk.strip():
                    results.append(chunk)
                    continue
                prefix = chunk[:len(chunk) - len(chunk.lstrip())]
                suffix = chunk[len(chunk.rstrip()):]
                results.append(prefix + self._translate_chunk(chunk.strip()) + suffix)
            translated = "".join(results)
            validate_markers(text, translated)
            return translated
        except Exception:
            with self._failure_lock:
                self.failed_count += 1
            raise

    def raise_if_failed(self):
        with self._failure_lock:
            failed = self.failed_count
        if failed:
            raise ValueError(f"Index-Translate 有 {failed} 段翻译失败，本次不能作为完整译文；请重试或更换引擎。")

    def close(self):
        self.session.close()
