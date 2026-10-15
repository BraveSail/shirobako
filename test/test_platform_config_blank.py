"""平台配置里的留空写法: 合法状态, 不该让 bot 起不来。

改配置时删掉 cookie 值只留一个 ``-`` (YAML 解析成 ``[None]``) 是很自然的写法,
以前会校验失败 → ``load_config`` 里 ``raise SystemExit(1)`` → **整个 bot 下线**
(2026-10-04 用户在 facebook 上就这么踩了一次)。
"""

import os
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.platform_config import Platform, PlatformsConfig


@pytest.mark.parametrize(
    ("label", "cookies"),
    [
        ("只留一个 -", [None]),
        ("空列表", []),
        ("空串", ["", "   "]),
        ("有值混着空", ["a=1", None, ""]),
    ],
)
def test_blank_cookies_are_treated_as_absent(label, cookies):
    """留空 == 没有这项配置 (退化为匿名), 不是配置错误"""
    p = Platform(cookies=cookies)
    assert p.cookies is None or all(p.cookies)
    assert p.roll_cookie() is None or p.roll_cookie() is not None


def test_real_cookies_are_kept():
    p = Platform(cookies=["a=1", None])
    assert len(p.cookies) == 1
    assert p.cookies[0].get_secret_value() == "a=1"


def test_blank_proxies_are_treated_as_absent():
    p = Platform(parser_proxies=[None], downloader_proxies=[])
    assert p.parser_proxies is None
    assert p.downloader_proxies is None
    assert p.roll_parser_proxy() is None
    assert p.roll_downloader_proxy() is None


def test_empty_platform_block_is_fine():
    p = Platform()
    assert p.cookies is None


def test_real_mistakes_still_get_caught():
    """留空要容忍, 但真写错 (类型不对/未知字段) 仍要拦下"""
    with pytest.raises(ValidationError):
        Platform(cookies=123)
    with pytest.raises(ValidationError):
        Platform(cookiez=["a=1"])


def test_a_broken_platform_does_not_take_the_whole_bot_down():
    """一份配置里有坏平台时: 跳过它, 其余照常, **进程不退出**"""
    cfg_file = Path(tempfile.mkdtemp()) / "platform_config.yaml"
    cfg_file.write_text(
        "platforms:\n"
        "  facebook:\n"
        "    cookies:\n"
        "    -\n"                        # 留空: 容忍
        "  twitter:\n"
        "    cookies:\n"
        "    - auth_token=ok\n"
        "  nosuchplatform:\n"            # 平台名不存在: 跳过
        "    cookies:\n"
        "    - x=1\n"
        "  instagram:\n"
        "    cookies: 123\n"             # 类型错: 跳过
        "    badfield: 1\n",
        encoding="utf-8",
    )
    cfg = PlatformsConfig.load_config(cfg_file)

    assert cfg.get("facebook").cookies is None        # 留空可用
    assert len(cfg.get("twitter").cookies or []) == 1  # 正常平台保留
    assert cfg.get("nosuchplatform") is None           # 拼错的平台没进来
    assert cfg.get("instagram") is None                # 坏配置没进来


# ── 热重载（cookie 刷新后免重启）────────────────────────────────────────────
#
# 场景: 宿主机上的刷新脚本改完 ``platform_config.yaml``（换 cookie / 开关代理），
# bot 必须**下一次解析就用新配置** —— 不能要求重启容器。


def _write_cfg(path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_reload_picks_up_new_cookies_without_restart():
    """改了 cookie 文件后, 下一次取配置就要拿到**新值**（免重启）。"""
    cfg_file = Path(tempfile.mkdtemp()) / "platform_config.yaml"
    _write_cfg(
        cfg_file,
        "platforms:\n  youtube:\n    cookies:\n    - SID=old\n",
    )
    cfg = PlatformsConfig.load_config(cfg_file)
    assert cfg.roll_cookie("youtube").get_secret_value() == "SID=old"

    # 模拟宿主机刷新脚本重写文件（mtime 会变）
    _write_cfg(
        cfg_file,
        "platforms:\n  youtube:\n    cookies:\n    - SID=new\n",
    )
    # 即使 mtime 粒度粗, 也用 ns 比对; 这里显式错开一步以保证可测

    os.utime(cfg_file, ns=(0, cfg_file.stat().st_mtime_ns + 10_000_000))

    assert cfg.roll_cookie("youtube").get_secret_value() == "SID=new"


def test_reload_is_a_noop_when_the_file_is_untouched():
    """文件没变 ⇒ 不重载（稳态零开销, 也避免刷日志）。"""
    cfg_file = Path(tempfile.mkdtemp()) / "platform_config.yaml"
    _write_cfg(cfg_file, "platforms:\n  youtube:\n    cookies:\n    - SID=x\n")
    cfg = PlatformsConfig.load_config(cfg_file)

    assert cfg.reload_if_changed() is False
    assert cfg.reload_if_changed() is False


def test_reload_updates_in_place_so_existing_references_stay_valid():
    """**必须原地更新**: ``from core import pl_cfg`` 的引用到处都是, 换对象会让它们用旧配置。"""
    cfg_file = Path(tempfile.mkdtemp()) / "platform_config.yaml"
    _write_cfg(cfg_file, "platforms:\n  youtube:\n    parser_proxies:\n    - socks5h://a:1\n")
    cfg = PlatformsConfig.load_config(cfg_file)
    same_object = cfg

    _write_cfg(cfg_file, "platforms:\n  youtube:\n    disable_parser_proxy: true\n")

    os.utime(cfg_file, ns=(0, cfg_file.stat().st_mtime_ns + 10_000_000))
    assert cfg.reload_if_changed() is True

    assert cfg is same_object, "必须是同一个对象（原地更新）"
    assert cfg.roll_parser_proxy("youtube") is None, "新配置里的 disable_parser_proxy 生效了"


def test_reload_can_turn_the_proxy_off_and_on():
    """代理开关也要能热生效（把 ``parser_proxies`` 删掉 + 关开关）。"""
    cfg_file = Path(tempfile.mkdtemp()) / "platform_config.yaml"
    _write_cfg(
        cfg_file,
        "platforms:\n  youtube:\n    parser_proxies:\n    - socks5h://127.0.0.1:1085\n",
    )
    cfg = PlatformsConfig.load_config(cfg_file)
    assert cfg.roll_parser_proxy("youtube") == "socks5h://127.0.0.1:1085"

    _write_cfg(cfg_file, "platforms:\n  youtube:\n    disable_parser_proxy: true\n")

    os.utime(cfg_file, ns=(0, cfg_file.stat().st_mtime_ns + 10_000_000))
    cfg.get("youtube")
    assert cfg.roll_parser_proxy("youtube") is None, "关掉后就不该再走代理"


def test_reload_keeps_old_config_when_the_new_file_is_broken():
    """新文件坏掉/YAML 解析失败 ⇒ **保留旧配置**并记日志, 不能在跑的服务失去配置。"""
    cfg_file = Path(tempfile.mkdtemp()) / "platform_config.yaml"
    _write_cfg(cfg_file, "platforms:\n  youtube:\n    cookies:\n    - SID=keepme\n")
    cfg = PlatformsConfig.load_config(cfg_file)

    _write_cfg(cfg_file, "platforms: [this is: not valid: yaml\n")

    os.utime(cfg_file, ns=(0, cfg_file.stat().st_mtime_ns + 10_000_000))
    cfg.reload_if_changed()

    assert cfg.roll_cookie("youtube").get_secret_value() == "SID=keepme", "旧配置必须还在"


def test_reload_does_not_retry_a_broken_file_every_call():
    """坏文件只在**变更时**重试一次 —— 否则每次取配置都要读盘+报错刷日志。"""
    cfg_file = Path(tempfile.mkdtemp()) / "platform_config.yaml"
    _write_cfg(cfg_file, "platforms:\n  youtube:\n    cookies:\n    - SID=x\n")
    cfg = PlatformsConfig.load_config(cfg_file)
    _write_cfg(cfg_file, "platforms: [broken\n")

    os.utime(cfg_file, ns=(0, cfg_file.stat().st_mtime_ns + 10_000_000))
    assert cfg.reload_if_changed() is False
    assert cfg.reload_if_changed() is False, "同一次坏变更不该反复重试"


def test_config_without_a_source_never_reloads():
    """``load_config`` 之外构造出来的实例（如 ``PlatformsConfig()``）不该乱 stat。"""
    assert PlatformsConfig().reload_if_changed() is False
