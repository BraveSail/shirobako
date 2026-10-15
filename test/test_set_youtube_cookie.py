"""``scripts/set_youtube_cookie.py`` —— 把刷新出来的 cookie 写进平台配置。

关键契约（都是"写坏了就出事"的点）:

- 写入必须**原子** —— 热重载按 mtime 触发, 读到半截文件会把好配置当成坏配置
- 写完必须**回读校验**并备份 —— 配置里有全平台的凭据, 不能改坏
- 默认**关掉代理**（按需求走直连）; ``--keep-proxy`` 保留
- 能读两种形态: ``.txt`` 的 ``k=v; k=v`` 串 与 ``.json`` 的对象
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "set_youtube_cookie.py"


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("set_youtube_cookie", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["set_youtube_cookie"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path, monkeypatch, mod):
    """把脚本的 CONFIG 指向临时仓库。"""
    cfg_dir = tmp_path / "data" / "config"
    cfg_dir.mkdir(parents=True)
    monkeypatch.setattr(mod, "CONFIG", cfg_dir / "platform_config.yaml")
    mod.CONFIG.write_text(
        "platforms:\n"
        "  youtube:\n"
        "    parser_proxies:\n"
        "    - socks5h://127.0.0.1:1085\n"
        "    downloader_proxies:\n"
        "    - socks5h://127.0.0.1:1085\n"
        "  twitter:\n"
        "    cookies:\n"
        "    - auth_token=keepme\n",
        encoding="utf-8",
    )
    return tmp_path


FULL_COOKIE = {
    "SID": "sid-value",
    "HSID": "hsid-value",
    "SSID": "ssid-value",
    "SAPISID": "sapisid-value",
    "__Secure-1PSID": "psid-value",
    "LOGIN_INFO": "login-info-value",
    "VISITOR_INFO1_LIVE": "visitor",
}

#: 用户实际给的那份**有效**登录态：**没有** SID/HSID/SSID/SAPISID（它们属于
#: .google.com，按域导出时不在），只有 ``LOGIN_INFO`` + ``__Secure-3PAPISID``。
#: 判据必须认它 —— 否则会把一份能用的 cookie 判成无效。
USER_SHAPE_COOKIE = {
    "LOGIN_INFO": "login-info-value",
    "__Secure-3PAPISID": "3papisid-value",
    "VISITOR_INFO1_LIVE": "visitor",
}


# ── 登录态判据 ──────────────────────────────────────────────────────────


def test_accepts_sapisid_shape(mod):
    assert mod.is_login_state(FULL_COOKIE)[0] is True


def test_accepts_the_user_shape_without_sid(mod):
    """只有 ``__Secure-3PAPISID``（无 ``SID``/``SAPISID``）也是登录态。"""
    ok, why = mod.is_login_state(USER_SHAPE_COOKIE)
    assert ok is True, why


def test_rejects_a_visitor_only_cookie(mod):
    ok, why = mod.is_login_state({"VISITOR_INFO1_LIVE": "x"})
    assert ok is False
    assert "LOGIN_INFO" in why


def test_rejects_when_login_info_missing_but_sapisid_present(mod):
    ok, why = mod.is_login_state({"SAPISID": "x"})
    assert ok is False
    assert "LOGIN_INFO" in why


# ── 解析 ────────────────────────────────────────────────────────────────


def test_parses_a_cookie_string(mod):
    got = mod.parse_cookie_text("SID=a; HSID=b;  SSID = c ;;")
    assert got == {"SID": "a", "HSID": "b", "SSID": "c"}


def test_parses_a_cookie_header_prefix(mod):
    assert mod.parse_cookie_text("Cookie: SID=a; HSID=b") == {"SID": "a", "HSID": "b"}


def test_loads_json_object(mod, tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text(json.dumps({"SID": "a"}), encoding="utf-8")
    assert mod.load_cookie_file(p) == {"SID": "a"}


def test_loads_the_refresh_scripts_nested_shape(mod, tmp_path):
    """刷新脚本导出的形态是嵌套的 ``{"cookies": {"name": {"value":…, "expires":…}}}``。

    直接按 ``name: value`` 解析会**拿到 dict 而不是字符串** —— 这条钉住兼容性。
    """
    payload = {
        "captured_at": 1792076400,
        "cookies": {
            "SID": {"value": "sid-value", "expires": 1807628475},
            "HSID": {"value": "hsid-value", "expires": -1},
        },
    }
    p = tmp_path / "cookies.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    assert mod.load_cookie_file(p) == {"SID": "sid-value", "HSID": "hsid-value"}


def test_loads_nested_shape_with_plain_values(mod, tmp_path):
    """嵌套但值是裸字符串时同样要能吃下（形态演进的中间态）。"""
    p = tmp_path / "cookies.json"
    p.write_text(json.dumps({"cookies": {"SID": "abc"}}), encoding="utf-8")
    assert mod.load_cookie_file(p) == {"SID": "abc"}


def test_loads_txt_string(mod, tmp_path):
    p = tmp_path / "cookies.txt"
    p.write_text("SID=a; HSID=b", encoding="utf-8")
    assert mod.load_cookie_file(p) == {"SID": "a", "HSID": "b"}


def test_rejects_a_non_object_json(mod, tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        mod.load_cookie_file(p)


# ── 写入 ────────────────────────────────────────────────────────────────


def test_writes_cookie_and_disables_the_proxy(mod, repo, monkeypatch):
    """默认行为: 写 cookie + 关掉 youtube 代理（走直连）。"""
    ck = repo / "c.json"
    ck.write_text(json.dumps(FULL_COOKIE), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["set_youtube_cookie.py", "--from-json", str(ck)])

    assert mod.main() == 0

    data = yaml.safe_load(mod.CONFIG.read_text(encoding="utf-8"))
    yt = data["platforms"]["youtube"]
    assert yt["cookies"] == [mod.as_cookie_string(FULL_COOKIE)]
    assert yt["disable_parser_proxy"] is True
    assert yt["disable_downloader_proxy"] is True
    assert "parser_proxies" not in yt, "关代理后不该再留着 proxies 键"
    assert "downloader_proxies" not in yt


def test_other_platforms_are_untouched(mod, repo, monkeypatch):
    """改 youtube 不能碰别的平台（配置里有全平台凭据）。"""
    ck = repo / "c.json"
    ck.write_text(json.dumps(FULL_COOKIE), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["x", "--from-json", str(ck)])
    mod.main()

    data = yaml.safe_load(mod.CONFIG.read_text(encoding="utf-8"))
    assert data["platforms"]["twitter"]["cookies"] == ["auth_token=keepme"]


def test_keep_proxy_flag_preserves_the_proxies(mod, repo, monkeypatch):
    ck = repo / "c.json"
    ck.write_text(json.dumps(FULL_COOKIE), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["x", "--from-json", str(ck), "--keep-proxy"])
    mod.main()

    yt = yaml.safe_load(mod.CONFIG.read_text(encoding="utf-8"))["platforms"]["youtube"]
    assert yt["parser_proxies"] == ["socks5h://127.0.0.1:1085"]
    assert "disable_parser_proxy" not in yt


def test_a_backup_is_left_behind(mod, repo, monkeypatch):
    ck = repo / "c.json"
    ck.write_text(json.dumps(FULL_COOKIE), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["x", "--from-json", str(ck)])
    mod.main()

    backups = list(mod.CONFIG.parent.glob(f"{mod.CONFIG.name}.bak-yt-*"))
    assert backups, "必须留备份"
    # 备份内容是改之前的样子
    old = yaml.safe_load(backups[0].read_text(encoding="utf-8"))
    assert "cookies" not in old["platforms"]["youtube"]


def test_write_is_atomic_no_temp_files_left(mod, repo, monkeypatch):
    """写完不能留临时文件（否则热重载可能扫到半截内容）。"""
    ck = repo / "c.json"
    ck.write_text(json.dumps(FULL_COOKIE), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["x", "--from-json", str(ck)])
    mod.main()

    leftovers = [p for p in mod.CONFIG.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_config_keeps_private_permissions(mod, repo, monkeypatch):
    ck = repo / "c.json"
    ck.write_text(json.dumps(FULL_COOKIE), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["x", "--from-json", str(ck)])
    mod.main()

    assert (mod.CONFIG.stat().st_mode & 0o077) == 0, "配置里有凭据, 权限必须是 600"


def test_warns_but_writes_when_login_markers_are_missing(mod, repo, monkeypatch, capsys):
    """不是登录态只警告不阻断 —— 但要让调用方看见。"""
    ck = repo / "c.json"
    ck.write_text(json.dumps({"VISITOR_INFO1_LIVE": "only-visitor"}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["x", "--from-json", str(ck)])

    assert mod.main() == 0
    assert "不是登录态" in capsys.readouterr().err


def test_does_not_warn_for_the_user_shape_cookie(mod, repo, monkeypatch, capsys):
    """只有 ``LOGIN_INFO`` + ``__Secure-3PAPISID`` 的有效 cookie **不该**报警告。"""
    ck = repo / "c.json"
    ck.write_text(json.dumps(USER_SHAPE_COOKIE), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["x", "--from-json", str(ck)])

    assert mod.main() == 0
    assert "不是登录态" not in capsys.readouterr().err


def test_missing_file_is_rejected(mod, repo, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["x", "/nonexistent/cookies.txt"])
    assert mod.main() == 2


def test_empty_cookie_is_rejected(mod, repo, monkeypatch):
    ck = repo / "c.txt"
    ck.write_text("   ", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["x", str(ck)])
    assert mod.main() == 2


def test_check_mode_changes_nothing(mod, repo, monkeypatch, capsys):
    before = mod.CONFIG.read_text(encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["x", "--check"])

    assert mod.main() == 0
    assert mod.CONFIG.read_text(encoding="utf-8") == before, "--check 不该改文件"
    out = capsys.readouterr().out
    assert "youtube.cookies" in out
