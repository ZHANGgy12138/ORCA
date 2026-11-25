"""Keep Bing requests within its limit without discarding long paragraphs."""

import re


def split_text(text, limit=900):
    while len(text) > limit:
        head = text[:limit]
        boundaries = list(re.finditer(r"[.!?;]\s+|\n+", head))
        if boundaries:
            end = boundaries[-1].end()
        else:
            whitespace = list(re.finditer(r"\s+", head))
            end = whitespace[-1].end() if whitespace else limit
        yield text[:end]
        text = text[end:]
    if text:
        yield text


def install():
    from pdf2zh_next.translator.translator_impl.bing import BingTranslator

    if getattr(BingTranslator, "_local_long_paragraphs", False):
        return
    original_init = BingTranslator.__init__
    original_translate = BingTranslator.do_translate

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.add_cache_impact_parameters("local_long_paragraphs", "split-v1")
        original_request = self.session.request

        def request(*args, **kwargs):
            kwargs.setdefault("timeout", (15, 45))
            return original_request(*args, **kwargs)

        self.session.request = request

    def translate(self, text, rate_limit_params=None):
        if len(text) <= 1000:
            return original_translate(self, text, rate_limit_params)
        translations = []
        for index, chunk in enumerate(split_text(text)):
            if index:
                self.rate_limiter.wait(rate_limit_params)
            translations.append(original_translate(self, chunk, rate_limit_params))
        return " ".join(translations)

    BingTranslator.__init__ = init
    BingTranslator.do_translate = translate
    BingTranslator._local_long_paragraphs = True
