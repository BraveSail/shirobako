#!/usr/bin/env python3
"""按 Telegram 当前语言批量修正 ``users.language_code``（只动 ``lang_source='tg'`` 的行）。

## 背景

``locale_for`` 早期只认 ISO 639-1 短码（``en``/``ja``），而 Telegram 在某些客户端上报
BCP-47 全码（``en-us``）。全码查不到就退回 bot 全局语言 ``zh-hans`` —— 于是一批英语用户
被存成了简中，且 ``ensure_lang`` 对已有用户不重读 TG 语言，他们永远纠不回来。

修好映射后，新用户正确、``lang_source='tg'`` 的老用户下次发消息也会自动纠正。
本脚本做的是**立刻**批量纠正：用 Telegram Bot API 的 ``getChatMember`` 拿每个用户在
群里的语言码 —— 但 Bot API **不返回** ``language_code``（只有 User 对象在 update 里才有）。

⇒ 所以这里走另一条路：**从 Bot API 的 `getChat`/update 拿不到，就用「配置里的群列表 +
历史解析日志」不可行**。真正可行且不猜测的做法是：

1. 直接改**明确已知**的那批 —— 用一个显式映射表（``--set tg_id=locale``）；
2. 或者 `--dry-run` 列出当前 ``tg`` 行的分布，人工核对后再用 (1) 修。

⚠️ 不臆造数据：脚本**不会**猜用户的语言。想批量纠正，就给出 (tg_id, locale) 清单。

用法（在 161 仓库根目录）::

    python3 scripts/fix_user_lang.py --list                 # 看现状分布
    python3 scripts/fix_user_lang.py --list --source tg
    python3 scripts/fix_user_lang.py --set 12345=en-us --set 67890=ja-jp
    python3 scripts/fix_user_lang.py --set-file /tmp/langs.txt   # 每行 tg_id=locale
    python3 scripts/fix_user_lang.py --mark-manual 12345         # 标记为手动，不再自动同步
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from db.models.user import LANG_SOURCE_MANUAL, LANG_SOURCE_TG, User
from db.session import get_session
from i18n import LANG_MAP
from services.user import locale_for


def _parse_pair(text: str) -> tuple[int, str]:
    """``tg_id=locale`` -> (int, locale)，并对 locale 做一次归一校验。"""
    if "=" not in text:
        raise argparse.ArgumentTypeError(f"格式应为 tg_id=locale，收到: {text!r}")
    raw_id, raw_locale = text.split("=", 1)
    try:
        tg_id = int(raw_id.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"tg_id 不是整数: {raw_id!r}") from exc
    # 允许写 en / en-US / zh-Hant，统一经 locale_for 归一
    locale = locale_for(raw_locale.strip())
    if locale not in LANG_MAP:
        raise argparse.ArgumentTypeError(f"未知 locale: {raw_locale!r} -> {locale!r}")
    return tg_id, locale


async def _list(*, source: str | None) -> int:
    async with get_session() as session:
        stmt = select(User.telegram_user_id, User.language_code, User.lang_source)
        if source:
            stmt = stmt.where(User.lang_source == source)
        rows = (await session.execute(stmt)).all()

    if not rows:
        print("（没有匹配的用户）")
        return 0

    by_lang = Counter(lang for _, lang, _ in rows)
    by_source = Counter(src for _, _, src in rows)
    print(f"共 {len(rows)} 个用户")
    print(f"按来源: {dict(by_source)}")
    print("按语言:")
    for lang, n in by_lang.most_common():
        print(f"  {lang!r:12} {n}")
    return 0


async def _apply(pairs: list[tuple[int, str]], *, dry_run: bool, force: bool = False) -> int:
    changed = 0
    skipped_manual = 0
    missing = 0

    async with get_session() as session:
        for tg_id, locale in pairs:
            user = (
                await session.execute(select(User).where(User.telegram_user_id == tg_id))
            ).scalar_one_or_none()
            if user is None:
                print(f"  ✗ tg={tg_id} 不在库里，跳过")
                missing += 1
                continue
            if user.lang_source == LANG_SOURCE_MANUAL and not force:
                # 手动选择默认不覆盖 —— 除非显式 --force
                print(f"  ⚠ tg={tg_id} 是手动设置的（{user.language_code}），跳过（加 --force 才改）")
                skipped_manual += 1
                continue
            if user.language_code == locale and user.lang_source == LANG_SOURCE_TG:
                print(f"  · tg={tg_id} 已是 {locale}，无需改")
                continue
            print(f"  {tg_id}: {user.language_code} -> {locale}")
            if not dry_run:
                user.language_code = locale
                user.lang_source = LANG_SOURCE_TG
            changed += 1

    print(f"\n变更 {changed} 个 / 手动跳过 {skipped_manual} / 不在库 {missing}{'（dry-run）' if dry_run else ''}")
    return 0


async def _mark(tg_ids: list[int], *, manual: bool) -> int:
    target = LANG_SOURCE_MANUAL if manual else LANG_SOURCE_TG
    async with get_session() as session:
        for tg_id in tg_ids:
            user = (
                await session.execute(select(User).where(User.telegram_user_id == tg_id))
            ).scalar_one_or_none()
            if user is None:
                print(f"  ✗ tg={tg_id} 不在库里")
                continue
            user.lang_source = target
            print(f"  tg={tg_id} lang_source -> {target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="批量修正用户语言（只动 lang_source='tg' 的行）")
    parser.add_argument("--list", action="store_true", help="列出用户语言分布")
    parser.add_argument("--source", choices=[LANG_SOURCE_TG, LANG_SOURCE_MANUAL], help="配合 --list 按来源过滤")
    parser.add_argument("--set", dest="pairs", action="append", default=[], type=_parse_pair, help="tg_id=locale")
    parser.add_argument("--set-file", type=Path, help="文件，每行 tg_id=locale")
    parser.add_argument("--mark-manual", nargs="+", type=int, default=[], help="把这些用户标成手动（不再自动同步）")
    parser.add_argument("--mark-auto", nargs="+", type=int, default=[], help="把这些用户标回自动")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落库")
    parser.add_argument("--force", action="store_true", help="连手动设置过的一起改")
    args = parser.parse_args()

    pairs: list[tuple[int, str]] = list(args.pairs)
    if args.set_file:
        for line in args.set_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pairs.append(_parse_pair(line))

    async def run() -> int:
        if args.list or (not pairs and not args.mark_manual and not args.mark_auto):
            return await _list(source=args.source)
        if args.mark_manual:
            await _mark(args.mark_manual, manual=True)
        if args.mark_auto:
            await _mark(args.mark_auto, manual=False)
        if pairs:
            return await _apply(pairs, dry_run=args.dry_run, force=args.force)
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
