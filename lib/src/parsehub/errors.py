class ParseHubError(Exception):
    MSG = __doc__

    def __init__(self, msg: str = ""):
        super().__init__(self.MSG if not msg else f"{self.MSG}: {msg}")


class ParseError(ParseHubError):
    """解析错误"""

    MSG = __doc__


class DownloadError(ParseHubError):
    """下载错误"""

    MSG = __doc__


class DeleteError(ParseHubError):
    """删除文件错误"""

    MSG = __doc__


class UnknownPlatform(ParseHubError):
    """不支持的平台"""

    MSG = __doc__


class TranslationUnavailable(ParseHubError):
    """翻译不可用（缺登录态 / 平台拒绝 / 没有译文）"""

    MSG = __doc__
