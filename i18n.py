import os

from easy_ai18n import EasyAI18n
from easy_ai18n.translators import LLMBulkTranslator

from core import bs

i18n = EasyAI18n(source_locale="zh-hans", func_names=["t_", "_t"])

t_ = i18n.i18n()

LANG_MAP = {
    "zh-hans": "简体中文",
    "zh-hant": "繁体中文",
    "en-us": "English",
    "ja-jp": "日本語",
    "ko-kr": "한국어",
    "fr-fr": "Français",
    "de-de": "Deutsch",
    "es-es": "Español",
    "pt-br": "Português (Brasil)",
    "ru-ru": "Русский",
    "it-it": "Italiano",
    "nl-nl": "Nederlands",
    "pl-pl": "Polski",
    "tr-tr": "Türkçe",
    "vi-vn": "Tiếng Việt",
    "th-th": "ภาษาไทย",
    "id-id": "Bahasa Indonesia",
}

#: Telegram 上报的用户语言码 (``User.language_code``) -> 本项目 locale。
#:
#: ⚠️ **必须同时登记 ISO 639-1 短码和 BCP-47 全码**：Telegram 上报的形态
#: **因客户端和版本而异** —— 实测同一套代码里，日语用户报 ``ja``、英语用户报
#: ``en-us``。只登记短码时 ``en-us`` 查不到 → 退回 bot 全局语言（简中），
#: 症状是「TG 明明是英语，i18n 却是中文」，而日语用户因为报的是 ``ja`` 恰好正常
#: （这个「日语正常、英语变中文」的不对称正是当年漏掉全码的原因）。
#:
#: 除 ``zh-hant`` 之外，带地区的全码一律映射到本项目的规范 locale。
#: **``zh-hant`` 必须排在 ``zh`` 之前语义上优先** —— 繁简是两种语言，
#: 不能靠 ``zh`` 前缀套用（这也是 ``locale_for`` 先查全串、再退主语言的原因）。
ISO639_MAP = {
    "": bs.language,
    # —— 中文（繁简单列，绝不互相回退）——
    "zh": "zh-hans",
    "zh-hans": "zh-hans",
    "zh-cn": "zh-hans",
    "zh-sg": "zh-hans",
    "zh-hant": "zh-hant",
    "zh-tw": "zh-hant",
    "zh-hk": "zh-hant",
    "zh-mo": "zh-hant",
    # —— 其余 ——
    "ja": "ja-jp",
    "ja-jp": "ja-jp",
    "en": "en-us",
    "en-us": "en-us",
    "en-gb": "en-us",
    "en-au": "en-us",
    "en-ca": "en-us",
    "ko": "ko-kr",
    "ko-kr": "ko-kr",
    "fr": "fr-fr",
    "fr-fr": "fr-fr",
    "de": "de-de",
    "de-de": "de-de",
    "es": "es-es",
    "es-es": "es-es",
    "pt": "pt-br",
    "pt-br": "pt-br",
    "pt-pt": "pt-br",
    "ru": "ru-ru",
    "ru-ru": "ru-ru",
    "it": "it-it",
    "it-it": "it-it",
    "nl": "nl-nl",
    "nl-nl": "nl-nl",
    "pl": "pl-pl",
    "pl-pl": "pl-pl",
    "tr": "tr-tr",
    "tr-tr": "tr-tr",
    "vi": "vi-vn",
    "vi-vn": "vi-vn",
    "th": "th-th",
    "th-th": "th-th",
    "id": "id-id",
    "id-id": "id-id",
}

if __name__ == "__main__":
    i18n.build(
        to_locales=list(LANG_MAP.keys()),
        translator=LLMBulkTranslator(model=os.getenv("I18N_MODEL", "gpt-4.1-mini")),
    )
