import random
from pathlib import Path

from parsehub.types import Platform as PPlatform
from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    PrivateAttr,
    SecretStr,
    field_serializer,
    field_validator,
)
from yaml import safe_load

from log import logger
from utils.helpers import mask_secret

from .config import bs

logger = logger.bind(name="PlatformConfig")


class MaskedSecretStr(SecretStr):
    def _display(self) -> str:
        value = self._secret_value
        return mask_secret(value)


def _drop_blank_entries(value: object) -> object:
    """列表字段里的空条目一律当作"没填", 全空则归一成 None。

    用户改配置时留空是常见写法 —— 删掉值只留一个 ``-`` (YAML 解析成 ``[None]``)、
    写成 ``[]``、或整行空着。这些**都是合法状态**(该平台退化为匿名访问),
    以前却会让平台校验失败 → ``load_config`` 直接 ``raise SystemExit(1)``
    → **整个 bot 起不来** (2026-10-04 用户删 facebook 的 cookie 时踩到)。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        kept = [v for v in value if v is not None and (not isinstance(v, str) or v.strip())]
        return kept or None
    return value


class Platform(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disable_parser_proxy: bool = False
    disable_downloader_proxy: bool = False
    parser_proxies: list[AnyUrl] | None = None
    downloader_proxies: list[AnyUrl] | None = None
    cookies: list[MaskedSecretStr] | None = None

    @field_validator("cookies", "parser_proxies", "downloader_proxies", mode="before")
    @classmethod
    def _ignore_blank_entries(cls, value: object) -> object:
        """留空 (``-`` / ``[]`` / 空串) 等同于"没有配置这一项", 而不是配置错误。"""
        return _drop_blank_entries(value)

    @field_serializer("cookies")
    def serialize_cookies(self, cookies: list[SecretStr] | None) -> list[str] | None:
        if cookies is None:
            return None
        return [str(cookie) for cookie in cookies]

    def roll_cookie(self) -> MaskedSecretStr | None:
        if not self.cookies:
            return None
        return random.choice(self.cookies)

    def roll_parser_proxy(self) -> str | None:
        if not self.parser_proxies:
            return None
        return str(random.choice(self.parser_proxies))

    def roll_downloader_proxy(self) -> str | None:
        if not self.downloader_proxies:
            return None
        return str(random.choice(self.downloader_proxies))


class PlatformsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_parser_proxies: list[AnyUrl] | None = None
    default_downloader_proxies: list[AnyUrl] | None = None
    platforms: dict[str, Platform] = {}

    _source: Path | None = PrivateAttr(default=None)
    """配置文件路径 —— 热重载要拿它重新 stat / 读盘。"""
    _mtime_ns: int = PrivateAttr(default=-1)
    """上次加载时文件的 ``st_mtime_ns``；``-1`` 表示尚未记录（首次必重载）。"""

    @classmethod
    def load_config(cls, file: Path) -> "PlatformsConfig":
        if not file.exists():
            logger.info("未找到 platform_config.yaml, 跳过加载")
            return cls()

        with open(file, encoding="utf-8") as f:
            data = safe_load(f)

        if not data:
            logger.info("platform_config.yaml 为空, 跳过加载")
            return cls()

        platforms = {}
        if data.get("platforms"):
            pid_list = [p.id for p in PPlatform]
            for name, pdata in data["platforms"].items():
                if name not in pid_list:
                    # 不 exit: 平台名写错只该让这一个失效, 不该让整个 bot 起不来
                    logger.error(f"平台 [{name}] 不存在, 已跳过 (支持的平台id: {pid_list})")
                    continue

                if not pdata:
                    continue

                try:
                    platforms[name] = Platform(**pdata)
                except Exception as e:
                    # 不 raise SystemExit: 单个平台的配置笔误不该让**整个 bot 下线**
                    # (服务着 22 个平台, 一处写错就全挂的代价太大)。跳过它,
                    # 该平台退化为无配置 (匿名) 运行, 日志里留下原因。
                    logger.error(f"平台 [{name}] 配置错误, 已跳过:\n{e}")
                    continue

        pc = cls(
            default_parser_proxies=cls._2l(data.get("default_parser_proxies", None)),
            default_downloader_proxies=cls._2l(data.get("default_downloader_proxies", None)),
            platforms=platforms,
        )
        pc._source = file  # noqa: SLF001 - 记下来源, 供热重载使用
        pc._mtime_ns = cls._stat_mtime_ns(file)  # noqa: SLF001
        logger.debug(f"已载入平台配置: {pc.model_dump_json(indent=4)}")
        return pc

    @staticmethod
    def _stat_mtime_ns(file: Path) -> int:
        """取文件 mtime（纳秒）。**用 ns 不用秒** —— 同一秒内写入会漏掉变更。"""
        try:
            return file.stat().st_mtime_ns
        except OSError:
            return -1

    def reload_if_changed(self) -> bool:
        """配置文件变了就**原地**重新加载，返回是否发生了重载。

        **为什么原地改**: ``pl_cfg`` 被 ``from core import pl_cfg`` 到处引用 ——
        换成新对象的话, 别处持有的旧引用会一直用老配置。
        所以这里只更新字段, 不动对象本身。

        惰性触发（每次取配置时比对 mtime）而不是 SIGHUP:
        刷新脚本改完文件后**下一次解析自动生效**, 不需要重启容器、也不需要外部发信号。
        ``stat`` 是纯本地调用（不产生网络请求）, 稳态开销可忽略。

        读盘/解析失败时**保留旧配置**并记日志 —— 半个坏文件不该让在跑的服务失去配置。
        """
        source = self._source  # noqa: SLF001
        if source is None:
            return False
        mtime = self._stat_mtime_ns(source)
        if mtime == self._mtime_ns:  # noqa: SLF001
            return False
        try:
            fresh = type(self).load_config(source)
        except Exception as e:  # noqa: BLE001 - 坏配置不能让在跑的服务挂掉
            logger.error(f"热重载失败, 继续用旧配置: {type(e).__name__}: {e}")
            # 记下这份坏文件的 mtime, 避免每次取配置都重试读盘刷日志
            self._mtime_ns = mtime  # noqa: SLF001
            return False

        self.default_parser_proxies = fresh.default_parser_proxies
        self.default_downloader_proxies = fresh.default_downloader_proxies
        self.platforms = fresh.platforms
        self._mtime_ns = mtime  # noqa: SLF001
        logger.info(f"平台配置已热重载 ({len(self.platforms)} 个平台)")
        return True

    @staticmethod
    def _2l[T](v: T | list[T] | None) -> list[T] | None:
        if v is None:
            return None
        if isinstance(v, list):
            return v
        return [v]

    def get(self, platform_id: str) -> Platform | None:
        self.reload_if_changed()
        return self.platforms.get(platform_id)

    def roll_cookie(self, platform_id: str) -> MaskedSecretStr | None:
        if not (pc := self.get(platform_id)):
            return None
        return pc.roll_cookie()

    def roll_parser_proxy(self, platform_id: str) -> str | None:
        if not (pc := self.get(platform_id)):
            pc = Platform()
        if pc.disable_parser_proxy:
            return None

        if platform_proxy := pc.roll_parser_proxy():
            return platform_proxy
        if self.default_parser_proxies:
            return str(random.choice(self.default_parser_proxies))
        return None

    def roll_downloader_proxy(self, platform_id: str) -> str | None:
        if not (pc := self.get(platform_id)):
            pc = Platform()
        if pc.disable_downloader_proxy:
            return None

        if platform_proxy := pc.roll_downloader_proxy():
            return platform_proxy
        if self.default_downloader_proxies:
            return str(random.choice(self.default_downloader_proxies))
        return None


pl_cfg = PlatformsConfig.load_config(bs.config_path / "platform_config.yaml")
