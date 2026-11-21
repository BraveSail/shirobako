from dataclasses import dataclass
from pathlib import Path

from ..utils.media_info import MediaInfoReader


@dataclass(kw_only=True)
class MediaFile:
    """本地媒体
    Attributes:
        path: 路径
        width: 宽度
        height: 高度
    """

    path: str | Path
    width: int = 0
    height: int = 0

    def __post_init__(self) -> None:
        if not self.width:
            info = MediaInfoReader.read(path=self.path)
            self.width = info.width
            self.height = info.height

    def exists(self) -> bool:
        """检查文件是否存在"""
        return Path(self.path).exists()


@dataclass(kw_only=True)
class VideoFile(MediaFile):
    """本地视频
    Attributes:
        path: 路径
        width: 宽度
        height: 高度
        duration: 视频时长，单位: 秒
    """

    duration: int = 0

    def __post_init__(self) -> None:
        if not self.width or not self.duration:
            info = MediaInfoReader.read(path=self.path)
            self.width = info.width
            self.height = info.height
            self.duration = info.duration


@dataclass(kw_only=True)
class ImageFile(MediaFile):
    """本地图片
    Attributes:
        path: 路径
        width: 宽度
        height: 高度
    """


@dataclass(kw_only=True)
class BrokenMedia(MediaFile):
    """**下载失败**的媒体占位。

    图床常对服务器 IP 限流（429）或返回 HTML 错误页 —— 此时内容不是图片，
    不能当图片发出去。但**也不能把它从列表里删掉**：``media_refs`` 与
    ``processed_list`` 是按位置 ``zip`` 的，删一项会让后面所有媒体错位
    （把 A 图显示到 B 的位置）。

    所以保留这个占位项，带着**原 URL**，由渲染层把 ``{{media:N}}``
    降级成一个链接。``path`` 为空即表示"没下载成功"。
    """

    url: str = ""

    def __post_init__(self) -> None:
        # 不读文件信息 —— 根本没有文件
        self.width = self.width or 0
        self.height = self.height or 0


@dataclass(kw_only=True)
class AniFile(MediaFile):
    """本地动图
    Attributes:
        path: 路径
        width: 宽度
        height: 高度
        duration: 视频时长，单位: 秒
    """

    duration: int = 0

    def __post_init__(self) -> None:
        if not self.width or not self.duration:
            info = MediaInfoReader.read(path=self.path)
            self.width = info.width
            self.height = info.height
            self.duration = info.duration


@dataclass(kw_only=True)
class LivePhotoFile(MediaFile):
    """本地 Live Photo
    Attributes:
        path: 图片路径
        width: 宽度
        height: 高度
        video_path: 视频路径
        duration: 视频时长，单位: 秒
    """

    video_path: str | Path | None = None
    duration: int = 3

    def __post_init__(self) -> None:
        if not self.width or not self.duration:
            info = MediaInfoReader.read(path=self.video_path or self.path)
            self.width = info.width
            self.height = info.height
            self.duration = info.duration


AnyMediaFile = MediaFile | VideoFile | ImageFile | AniFile | LivePhotoFile

__all__ = ["MediaFile", "VideoFile", "ImageFile", "AniFile", "LivePhotoFile", "AnyMediaFile"]
