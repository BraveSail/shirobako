#!/usr/bin/env python3
"""给 welcome 文案补齐 16 语言词条。

这些句子原先嵌在带 ``{get_supported_platforms()}`` 的 f-string 里，i18n 的静态
提取抓不到，所以从未进过词条表 —— ``t_[lang]`` 取任何语言都回退源文本。

键 = md5(源文本)[:12]（与 easy_ai18n 的 ``Text.id_of`` 一致）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

#: 源文本 -> {locale: 译文}
ENTRIES: dict[str, dict[str, str]] = {
    "**发送分享链接以进行解析**": {
        "zh-hant": "**傳送分享連結以進行解析**",
        "en-us": "**Send a share link to parse it**",
        "ja-jp": "**共有リンクを送信して解析**",
        "ko-kr": "**공유 링크를 보내면 파싱합니다**",
        "fr-fr": "**Envoyez un lien de partage pour l'analyser**",
        "de-de": "**Senden Sie einen Freigabelink zum Parsen**",
        "es-es": "**Envía un enlace compartido para analizarlo**",
        "pt-br": "**Envie um link compartilhado para analisar**",
        "ru-ru": "**Отправьте ссылку для разбора**",
        "it-it": "**Invia un link condiviso per analizzarlo**",
        "nl-nl": "**Stuur een deellink om te parsen**",
        "pl-pl": "**Wyślij link do udostępnienia, aby go przetworzyć**",
        "tr-tr": "**Ayrıştırmak için bir paylaşım bağlantısı gönderin**",
        "vi-vn": "**Gửi liên kết chia sẻ để phân tích**",
        "th-th": "**ส่งลิงก์แชร์เพื่อแยกวิเคราะห์**",
        "id-id": "**Kirim tautan berbagi untuk diurai**",
    },
    "**支持的平台:**": {
        "zh-hant": "**支援的平台:**",
        "en-us": "**Supported platforms:**",
        "ja-jp": "**対応プラットフォーム:**",
        "ko-kr": "**지원 플랫폼:**",
        "fr-fr": "**Plateformes prises en charge :**",
        "de-de": "**Unterstützte Plattformen:**",
        "es-es": "**Plataformas compatibles:**",
        "pt-br": "**Plataformas suportadas:**",
        "ru-ru": "**Поддерживаемые платформы:**",
        "it-it": "**Piattaforme supportate:**",
        "nl-nl": "**Ondersteunde platforms:**",
        "pl-pl": "**Obsługiwane platformy:**",
        "tr-tr": "**Desteklenen platformlar:**",
        "vi-vn": "**Nền tảng được hỗ trợ:**",
        "th-th": "**แพลตฟอร์มที่รองรับ:**",
        "id-id": "**Platform yang didukung:**",
    },
    "**命令列表:**": {
        "zh-hant": "**指令列表:**",
        "en-us": "**Commands:**",
        "ja-jp": "**コマンド一覧:**",
        "ko-kr": "**명령어 목록:**",
        "fr-fr": "**Commandes :**",
        "de-de": "**Befehle:**",
        "es-es": "**Comandos:**",
        "pt-br": "**Comandos:**",
        "ru-ru": "**Команды:**",
        "it-it": "**Comandi:**",
        "nl-nl": "**Opdrachten:**",
        "pl-pl": "**Polecenia:**",
        "tr-tr": "**Komutlar:**",
        "vi-vn": "**Lệnh:**",
        "th-th": "**คำสั่ง:**",
        "id-id": "**Perintah:**",
    },
    "**开源地址: [GitHub](https://github.com/BraveSail/shirobako)**": {
        "zh-hant": "**開源位址: [GitHub](https://github.com/BraveSail/shirobako)**",
        "en-us": "**Source: [GitHub](https://github.com/BraveSail/shirobako)**",
        "ja-jp": "**ソース: [GitHub](https://github.com/BraveSail/shirobako)**",
        "ko-kr": "**소스: [GitHub](https://github.com/BraveSail/shirobako)**",
        "fr-fr": "**Source : [GitHub](https://github.com/BraveSail/shirobako)**",
        "de-de": "**Quellcode: [GitHub](https://github.com/BraveSail/shirobako)**",
        "es-es": "**Código: [GitHub](https://github.com/BraveSail/shirobako)**",
        "pt-br": "**Código: [GitHub](https://github.com/BraveSail/shirobako)**",
        "ru-ru": "**Исходный код: [GitHub](https://github.com/BraveSail/shirobako)**",
        "it-it": "**Sorgente: [GitHub](https://github.com/BraveSail/shirobako)**",
        "nl-nl": "**Bron: [GitHub](https://github.com/BraveSail/shirobako)**",
        "pl-pl": "**Źródło: [GitHub](https://github.com/BraveSail/shirobako)**",
        "tr-tr": "**Kaynak: [GitHub](https://github.com/BraveSail/shirobako)**",
        "vi-vn": "**Mã nguồn: [GitHub](https://github.com/BraveSail/shirobako)**",
        "th-th": "**ซอร์สโค้ด: [GitHub](https://github.com/BraveSail/shirobako)**",
        "id-id": "**Sumber: [GitHub](https://github.com/BraveSail/shirobako)**",
    },
    # 命令清单：整块一条（含换行），避免逐条翻译时斜杠命令被改坏
    "/jx <链接> - 解析并预览内容\n"
    "/raw <链接> - 发送原文件, 避免画质压缩\n"
    "/zip <链接> - 打包发送, 附带解析信息\n"
    "/jxjx <链接> - 绕过缓存解析\n"
    "/cfg - 配置\n"
    "/cfg <频道用户名/链接/id> - 频道配置": {
        "zh-hant": "/jx <連結> - 解析並預覽內容\n"
        "/raw <連結> - 傳送原始檔案, 避免畫質壓縮\n"
        "/zip <連結> - 打包傳送, 附帶解析資訊\n"
        "/jxjx <連結> - 繞過快取解析\n"
        "/cfg - 設定\n"
        "/cfg <頻道使用者名稱/連結/id> - 頻道設定",
        "en-us": "/jx <link> - parse and preview the content\n"
        "/raw <link> - send the original file, avoiding compression\n"
        "/zip <link> - send as an archive with parse info\n"
        "/jxjx <link> - parse bypassing the cache\n"
        "/cfg - settings\n"
        "/cfg <channel username/link/id> - channel settings",
        "ja-jp": "/jx <リンク> - 解析してプレビュー\n"
        "/raw <リンク> - 元ファイルを送信（画質圧縮なし）\n"
        "/zip <リンク> - 解析情報付きでまとめて送信\n"
        "/jxjx <リンク> - キャッシュを迂回して解析\n"
        "/cfg - 設定\n"
        "/cfg <チャンネルユーザー名/リンク/id> - チャンネル設定",
        "ko-kr": "/jx <링크> - 파싱 및 미리보기\n"
        "/raw <링크> - 원본 파일 전송(화질 압축 없음)\n"
        "/zip <링크> - 파싱 정보와 함께 압축 전송\n"
        "/jxjx <링크> - 캐시 우회 파싱\n"
        "/cfg - 설정\n"
        "/cfg <채널 사용자명/링크/id> - 채널 설정",
        "fr-fr": "/jx <lien> - analyser et prévisualiser\n"
        "/raw <lien> - envoyer le fichier original, sans compression\n"
        "/zip <lien> - envoyer une archive avec les infos d'analyse\n"
        "/jxjx <lien> - analyser en contournant le cache\n"
        "/cfg - paramètres\n"
        "/cfg <nom/lien/id de la chaîne> - paramètres de la chaîne",
        "de-de": "/jx <Link> - parsen und Vorschau anzeigen\n"
        "/raw <Link> - Originaldatei senden, ohne Kompression\n"
        "/zip <Link> - als Archiv mit Parse-Infos senden\n"
        "/jxjx <Link> - unter Umgehung des Caches parsen\n"
        "/cfg - Einstellungen\n"
        "/cfg <Kanalname/Link/ID> - Kanaleinstellungen",
        "es-es": "/jx <enlace> - analizar y previsualizar\n"
        "/raw <enlace> - enviar el archivo original, sin compresión\n"
        "/zip <enlace> - enviar como archivo con información del análisis\n"
        "/jxjx <enlace> - analizar omitiendo la caché\n"
        "/cfg - configuración\n"
        "/cfg <nombre/enlace/id del canal> - configuración del canal",
        "pt-br": "/jx <link> - analisar e pré-visualizar\n"
        "/raw <link> - enviar o arquivo original, sem compressão\n"
        "/zip <link> - enviar como arquivo com informações da análise\n"
        "/jxjx <link> - analisar ignorando o cache\n"
        "/cfg - configurações\n"
        "/cfg <nome/link/id do canal> - configurações do canal",
        "ru-ru": "/jx <ссылка> - разобрать и показать превью\n"
        "/raw <ссылка> - отправить исходный файл без сжатия\n"
        "/zip <ссылка> - отправить архивом с данными разбора\n"
        "/jxjx <ссылка> - разобрать в обход кеша\n"
        "/cfg - настройки\n"
        "/cfg <имя/ссылка/id канала> - настройки канала",
        "it-it": "/jx <link> - analizza e mostra anteprima\n"
        "/raw <link> - invia il file originale, senza compressione\n"
        "/zip <link> - invia come archivio con le info di analisi\n"
        "/jxjx <link> - analizza ignorando la cache\n"
        "/cfg - impostazioni\n"
        "/cfg <nome/link/id del canale> - impostazioni del canale",
        "nl-nl": "/jx <link> - parsen en voorbeeld tonen\n"
        "/raw <link> - origineel bestand sturen, zonder compressie\n"
        "/zip <link> - als archief met parse-info sturen\n"
        "/jxjx <link> - parsen met omzeiling van de cache\n"
        "/cfg - instellingen\n"
        "/cfg <kanaalnaam/link/id> - kanaalinstellingen",
        "pl-pl": "/jx <link> - przetwórz i pokaż podgląd\n"
        "/raw <link> - wyślij oryginalny plik bez kompresji\n"
        "/zip <link> - wyślij jako archiwum z informacjami\n"
        "/jxjx <link> - przetwórz z pominięciem pamięci podręcznej\n"
        "/cfg - ustawienia\n"
        "/cfg <nazwa/link/id kanału> - ustawienia kanału",
        "tr-tr": "/jx <bağlantı> - ayrıştır ve önizle\n"
        "/raw <bağlantı> - orijinal dosyayı sıkıştırmadan gönder\n"
        "/zip <bağlantı> - ayrıştırma bilgisiyle arşiv olarak gönder\n"
        "/jxjx <bağlantı> - önbelleği atlayarak ayrıştır\n"
        "/cfg - ayarlar\n"
        "/cfg <kanal kullanıcı adı/bağlantı/id> - kanal ayarları",
        "vi-vn": "/jx <liên kết> - phân tích và xem trước\n"
        "/raw <liên kết> - gửi tệp gốc, không nén chất lượng\n"
        "/zip <liên kết> - gửi dạng gói kèm thông tin phân tích\n"
        "/jxjx <liên kết> - phân tích bỏ qua bộ nhớ đệm\n"
        "/cfg - cài đặt\n"
        "/cfg <tên/liên kết/id kênh> - cài đặt kênh",
        "th-th": "/jx <ลิงก์> - แยกวิเคราะห์และแสดงตัวอย่าง\n"
        "/raw <ลิงก์> - ส่งไฟล์ต้นฉบับ ไม่บีบอัดคุณภาพ\n"
        "/zip <ลิงก์> - ส่งเป็นไฟล์รวมพร้อมข้อมูลการแยกวิเคราะห์\n"
        "/jxjx <ลิงก์> - แยกวิเคราะห์โดยข้ามแคช\n"
        "/cfg - ตั้งค่า\n"
        "/cfg <ชื่อผู้ใช้/ลิงก์/id ของช่อง> - ตั้งค่าช่อง",
        "id-id": "/jx <tautan> - urai dan pratinjau\n"
        "/raw <tautan> - kirim file asli, tanpa kompresi\n"
        "/zip <tautan> - kirim sebagai arsip dengan info urai\n"
        "/jxjx <tautan> - urai dengan melewati cache\n"
        "/cfg - pengaturan\n"
        "/cfg <nama/tautan/id kanal> - pengaturan kanal",
    },
}


def key_of(src: str) -> str:
    return hashlib.md5(src.encode()).hexdigest()[:12]


def main() -> int:
    added = 0
    for yaml_path in sorted(ROOT.glob("i18n/*.yaml")):
        locale = yaml_path.stem
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        changed = False
        for src, translations in ENTRIES.items():
            if locale not in translations:
                continue
            k = key_of(src)
            if k in data:
                continue
            data[k] = translations[locale]
            changed = True
            added += 1
        if changed:
            yaml_path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=True, width=10_000),
                encoding="utf-8",
            )
            print(f"{locale}: 已更新")
    print(f"\n新增 {added} 条译文")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
