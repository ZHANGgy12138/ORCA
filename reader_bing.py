"""Bing reading adapter: reuse a normal web session, with bounded requests.

This is the public web-translation adapter, not the Azure Translator API.
Token reuse is a local short-lived optimization, not a promised server TTL.
Rate-limit responses are returned to the caller without refreshing or retrying.
"""
import time

import requests
from desktop_compat import split_text
from pdf2zh_next.translator.translator_impl.bing import BingTranslator


class ReaderBingTranslator(BingTranslator):
    TOKEN_TTL = 120
    REQUEST_TIMEOUT = (5, 12)

    def __init__(self, settings, rate_limiter):
        super().__init__(settings, rate_limiter)
        self.session.headers.update(self.headers)
        self._reader_tokens = None
        self._reader_tokens_at = 0
        self.add_cache_impact_parameters("reader_session", "v1")

    def find_sid(self):
        if self._reader_tokens and time.monotonic() - self._reader_tokens_at < self.TOKEN_TTL:
            return self._reader_tokens
        # The original parser follows normal redirects and takes the session
        # values returned by Bing's own translator page.
        response = self.session.get(self.endpoint, timeout=self.REQUEST_TIMEOUT)
        if response.status_code == 429:
            raise ValueError("Bing 暂时限制了翻译请求，请稍后再试或更换引擎。")
        response.raise_for_status()
        import re
        try:
            url = response.url[:-10]
            ig = re.findall(r'"ig":"(.*?)"', response.text)[0]
            iid = re.findall(r'data-iid="(.*?)"', response.text)[-1]
            key, token = re.findall(r'params_AbusePreventionHelper\s=\s\[(.*?),"(.*?)",', response.text)[0]
        except (IndexError, ValueError):
            raise ValueError("Bing 翻译网页暂时无法读取，请稍后重试或更换引擎。") from None
        self._reader_tokens = (url, ig, iid, key, token)
        self._reader_tokens_at = time.monotonic()
        return self._reader_tokens

    def _chunk(self, text):
        for attempt in range(2):
            url, ig, iid, key, token = self.find_sid()
            response = self.session.post(
                f"{url}ttranslatev3?IG={ig}&IID={iid}",
                data={"fromLang": self.lang_in, "to": self.lang_out, "text": text, "token": token, "key": key},
                headers=self.headers, timeout=self.REQUEST_TIMEOUT,
            )
            if response.status_code == 429:
                raise ValueError("Bing 暂时限制了翻译请求，请稍后再试或更换引擎。")
            # A stale normal web-session token can be replaced once. Never
            # retry 429, connection failures, server errors, or challenge HTML.
            if response.status_code in (400, 401) and attempt == 0 and "text/html" not in response.headers.get("Content-Type", "").lower():
                self._reader_tokens = None
                continue
            response.raise_for_status()
            try:
                result = response.json()
            except requests.exceptions.JSONDecodeError:
                raise ValueError("Bing 返回了无法读取的结果，请稍后重试。") from None
            if isinstance(result, dict):
                code = result.get("statusCode", result.get("error", {}).get("code") if isinstance(result.get("error"), dict) else None)
                if str(code) == "429":
                    raise ValueError("Bing 暂时限制了翻译请求，请稍后再试或更换引擎。")
                if str(code) in ("400", "401") and attempt == 0:
                    self._reader_tokens = None
                    continue
            try:
                translated = result[0]["translations"][0]["text"]
                if not isinstance(translated, str) or not translated.strip():
                    raise ValueError()
                return translated
            except (KeyError, IndexError, TypeError, ValueError):
                raise ValueError("Bing 未返回译文，请稍后重试或更换引擎。") from None
        raise ValueError("Bing 会话已失效，请重试。")

    def do_translate(self, text, rate_limit_params=None):
        chunks = list(split_text(text)) if len(text) > 1000 else [text]
        translated = []
        for index, chunk in enumerate(chunks):
            if index:
                self.rate_limiter.wait(rate_limit_params)
            translated.append(self._chunk(chunk))
        return " ".join(translated)
