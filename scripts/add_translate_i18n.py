#!/usr/bin/env python3
"""给 /t（翻译推文）相关的文案补齐 16 语言词条。

键 = md5(源文本)[:12]（与 easy_ai18n 的 ``Text.id_of`` 一致）；
源语言是简中，没有 zh-hans.yaml —— 简中直接显示源文本。

用法：``.venv/bin/python scripts/add_translate_i18n.py``
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "i18n"

#: 源文本 -> {locale: 译文}
ENTRIES: dict[str, dict[str, str]] = {
    "翻译推文": {
        "zh-hant": "翻譯推文",
        "en-us": "Translate a post",
        "ja-jp": "投稿を翻訳",
        "ko-kr": "게시물 번역",
        "fr-fr": "Traduire une publication",
        "de-de": "Beitrag übersetzen",
        "es-es": "Traducir una publicación",
        "pt-br": "Traduzir uma publicação",
        "ru-ru": "Перевести пост",
        "it-it": "Traduci un post",
        "nl-nl": "Een bericht vertalen",
        "pl-pl": "Przetłumacz post",
        "tr-tr": "Gönderiyi çevir",
        "vi-vn": "Dịch bài đăng",
        "th-th": "แปลโพสต์",
        "id-id": "Terjemahkan postingan",
    },
    "用法：/t <推文链接>": {
        "zh-hant": "用法：/t <推文連結>",
        "en-us": "Usage: /t <post link>",
        "ja-jp": "使い方: /t <投稿リンク>",
        "ko-kr": "사용법: /t <게시물 링크>",
        "fr-fr": "Utilisation : /t <lien de la publication>",
        "de-de": "Verwendung: /t <Beitragslink>",
        "es-es": "Uso: /t <enlace de la publicación>",
        "pt-br": "Uso: /t <link da publicação>",
        "ru-ru": "Использование: /t <ссылка на пост>",
        "it-it": "Uso: /t <link del post>",
        "nl-nl": "Gebruik: /t <link naar bericht>",
        "pl-pl": "Użycie: /t <link do posta>",
        "tr-tr": "Kullanım: /t <gönderi bağlantısı>",
        "vi-vn": "Cách dùng: /t <liên kết bài đăng>",
        "th-th": "วิธีใช้: /t <ลิงก์โพสต์>",
        "id-id": "Penggunaan: /t <tautan postingan>",
    },
    "翻译失败，请稍后再试": {
        "zh-hant": "翻譯失敗，請稍後再試",
        "en-us": "Translation failed, please try again later",
        "ja-jp": "翻訳に失敗しました。しばらくしてからもう一度お試しください",
        "ko-kr": "번역에 실패했습니다. 잠시 후 다시 시도해 주세요",
        "fr-fr": "Échec de la traduction, veuillez réessayer plus tard",
        "de-de": "Übersetzung fehlgeschlagen, bitte später erneut versuchen",
        "es-es": "Error de traducción, inténtalo más tarde",
        "pt-br": "Falha na tradução, tente novamente mais tarde",
        "ru-ru": "Не удалось перевести, попробуйте позже",
        "it-it": "Traduzione non riuscita, riprova più tardi",
        "nl-nl": "Vertaling mislukt, probeer het later opnieuw",
        "pl-pl": "Tłumaczenie nie powiodło się, spróbuj później",
        "tr-tr": "Çeviri başarısız oldu, lütfen daha sonra tekrar deneyin",
        "vi-vn": "Dịch thất bại, vui lòng thử lại sau",
        "th-th": "แปลไม่สำเร็จ โปรดลองอีกครั้งภายหลัง",
        "id-id": "Terjemahan gagal, silakan coba lagi nanti",
    },
    "X 翻译需要登录态（auth_token / ct0 缺失）": {
        "zh-hant": "X 翻譯需要登入狀態（auth_token / ct0 缺失）",
        "en-us": "X translation requires a login session (auth_token / ct0 missing)",
        "ja-jp": "X の翻訳にはログイン状態が必要です（auth_token / ct0 がありません）",
        "ko-kr": "X 번역에는 로그인 상태가 필요합니다 (auth_token / ct0 없음)",
        "fr-fr": "La traduction X nécessite une session connectée (auth_token / ct0 manquants)",
        "de-de": "Die X-Übersetzung erfordert eine Anmeldung (auth_token / ct0 fehlen)",
        "es-es": "La traducción de X requiere sesión iniciada (faltan auth_token / ct0)",
        "pt-br": "A tradução do X requer sessão iniciada (auth_token / ct0 ausentes)",
        "ru-ru": "Для перевода X нужен вход в аккаунт (нет auth_token / ct0)",
        "it-it": "La traduzione di X richiede l'accesso (auth_token / ct0 mancanti)",
        "nl-nl": "X-vertaling vereist een ingelogde sessie (auth_token / ct0 ontbreken)",
        "pl-pl": "Tłumaczenie X wymaga zalogowania (brak auth_token / ct0)",
        "tr-tr": "X çevirisi oturum açmayı gerektirir (auth_token / ct0 yok)",
        "vi-vn": "Dịch trên X cần đăng nhập (thiếu auth_token / ct0)",
        "th-th": "การแปลของ X ต้องเข้าสู่ระบบ (ไม่มี auth_token / ct0)",
        "id-id": "Terjemahan X memerlukan sesi masuk (auth_token / ct0 tidak ada)",
    },
    "X 没有返回译文（该推文可能不可翻译）": {
        "zh-hant": "X 沒有回傳譯文（該推文可能無法翻譯）",
        "en-us": "X returned no translation (this post may not be translatable)",
        "ja-jp": "X が翻訳を返しませんでした（この投稿は翻訳できない可能性があります）",
        "ko-kr": "X가 번역을 반환하지 않았습니다 (이 게시물은 번역할 수 없을 수 있습니다)",
        "fr-fr": "X n'a renvoyé aucune traduction (cette publication n'est peut-être pas traduisible)",
        "de-de": "X hat keine Übersetzung geliefert (dieser Beitrag ist evtl. nicht übersetzbar)",
        "es-es": "X no devolvió traducción (puede que esta publicación no sea traducible)",
        "pt-br": "O X não retornou tradução (talvez esta publicação não seja traduzível)",
        "ru-ru": "X не вернул перевод (возможно, этот пост нельзя перевести)",
        "it-it": "X non ha restituito alcuna traduzione (forse questo post non è traducibile)",
        "nl-nl": "X gaf geen vertaling terug (dit bericht is mogelijk niet vertaalbaar)",
        "pl-pl": "X nie zwrócił tłumaczenia (ten post może być nieprzetłumaczalny)",
        "tr-tr": "X çeviri döndürmedi (bu gönderi çevrilemiyor olabilir)",
        "vi-vn": "X không trả về bản dịch (bài đăng này có thể không dịch được)",
        "th-th": "X ไม่ส่งคำแปลกลับมา (โพสต์นี้อาจแปลไม่ได้)",
        "id-id": "X tidak mengembalikan terjemahan (postingan ini mungkin tidak dapat diterjemahkan)",
    },
}


def id_of(text: str) -> str:
    """与 easy_ai18n ``Text.id_of`` 一致的键。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


def main() -> int:
    added: dict[str, int] = {}
    for locale_file in sorted(I18N.glob("*.yaml")):
        locale = locale_file.stem
        data: dict[str, str] = yaml.safe_load(locale_file.read_text(encoding="utf-8")) or {}
        before = len(data)
        for source, translations in ENTRIES.items():
            translated = translations.get(locale)
            if not translated:
                continue
            data[id_of(source)] = translated
        if len(data) != before:
            locale_file.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=True, width=100000),
                encoding="utf-8",
            )
            added[locale] = len(data) - before
    print(f"已补齐 {sum(added.values())} 条（{len(added)} 个语言文件）")
    for k, v in sorted(added.items()):
        print(f"  {k}: +{v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
