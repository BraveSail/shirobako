"""YouTube 一个 parser 管两类链接：视频 / 音乐（自研取流）与社区帖子（页面数据）。

**视频 / 音乐自研, 不再依赖 yt-dlp**：GET ``watch?v=<id>`` 页面，从内嵌的
``ytInitialPlayerResponse`` 里拿**明文直链**（同时拿到 ``microformat`` 元数据），
再交给项目自研下载器（``utils/downloader.py``）。这条路就是浏览器走的路 ——
**别改成 POST ``/youtubei/v1/player``**：实测那条路对同一条视频一律 ``LOGIN_REQUIRED``,
而 watch 页给 31 条明文流（细节与受控对照见 ``provider_api/youtube_video.py``）。

高画质是**音视频分离**的，本文件用 ffmpeg ``-c copy`` 合并
（lib 之外 bot 侧本就在用 ffmpeg，见 ``utils/media_processing_unit.py``）。

帖子（``/post/<id>``）走另一条路 —— yt-dlp 会把 ``/post/<id>`` 当成频道 tab
（``[youtube:tab] post: This channel does not have a Ugk… tab``），所以帖子只能读页面里的
``ytInitialData``（见 ``provider_api/youtube.py``）。两条路径在这里按 URL 分派，对外仍是
同一个平台、同一个 parser。
"""

from __future__ import annotations

import asyncio
import html
import shutil
from pathlib import Path

from loguru import logger

from ...provider_api.youtube import (
    VIDEO_ID_RE,
    YoutubePostError,
    YoutubePostPoll,
    YoutubePostVideo,
    fetch_post,
    post_id_from_url,
)
from ...provider_api.youtube_sabr import (
    SabrFetchError,
    fetch_sabr_streams,
)
from ...provider_api.youtube_video import (
    DEFAULT_MAX_HEIGHT,
    SelectedStreams,
    YoutubeVideo,
    YoutubeVideoError,
    fetch_video,
    select_streams,
)
from ...types import (
    DownloadError,
    DownloadResult,
    ImageRef,
    MultimediaParseResult,
    ParseError,
    Platform,
    Poll,
    PollOption,
    ProgressCallback,
    VideoFile,
    VideoParseResult,
    VideoRef,
)
from ...utils.downloader import download
from ...utils.helpers import SecretCookie, image_ext_from_url, profile_url
from ..base.base import BaseParser


class YtbParse(BaseParser):
    __platform__ = Platform.YOUTUBE
    __supported_type__ = ["视频", "音乐", "图文"]
    # post 要匹配（早期版本把它排除了, 于是帖子落到「不支持的平台」）；
    # ``@handle`` 频道页仍不支持（那种页面拿不到视频本体）。
    __match__ = r"^(http(s)?://).*youtu(be|.be)?(\.com)?/(?!live)(?!@).+"
    __redirect_keywords__ = ["m.youtube.com"]
    __reserved_parameters__ = ["v", "list", "index"]

    async def _do_parse(self, raw_url: str) -> YtbVideoParseResult | MultimediaParseResult:
        """帖子走页面数据, 其余链接（视频/音乐）走自研 player API。"""
        if post_id_from_url(raw_url):
            return await self._parse_post(raw_url)
        return await self._parse_video(raw_url)

    # ------------------------------------------------------------- 视频 / 音乐

    async def _parse_video(self, raw_url: str) -> YtbVideoParseResult:
        match = VIDEO_ID_RE.search(raw_url)
        if not match:
            raise ParseError(f"无法从链接中解析出视频 ID: {raw_url}")
        cookie_value = self.cookie.get_value() if self.cookie else None
        try:
            video = await fetch_video(
                match.group(1),
                proxy=self.proxy,
                cookie=cookie_value,
            )
        except YoutubeVideoError as e:
            raise ParseError(f"无法解析 YouTube 视频: {e}") from e
        selected = select_streams(video, max_height=DEFAULT_MAX_HEIGHT, allow_mux=self._ffmpeg_available())
        return YtbVideoParseResult(
            info=video,
            selected=selected,
            cookie=str(SecretCookie(cookie_value)) if cookie_value else None,
        )

    @staticmethod
    def _ffmpeg_available() -> bool:
        """有 ffmpeg 才能 mux 分离流；没有就只能退回合一的 360p 单文件。"""
        return shutil.which("ffmpeg") is not None

    # ------------------------------------------------------------------ 帖子

    async def _parse_post(self, raw_url: str) -> MultimediaParseResult:
        try:
            post = await fetch_post(
                raw_url,
                proxy=self.proxy,
                cookie=self.cookie.get_value() if self.cookie else None,
            )
        except YoutubePostError as e:
            raise ParseError(f"无法获取帖子内容: {e}") from e
        except Exception as e:  # noqa: BLE001 - 网络/解析异常都要翻译成 ParseError
            raise ParseError(f"无法获取帖子内容: {e}") from e

        media: list[ImageRef] = [
            ImageRef(
                url=image.url,
                thumb_url=image.thumb_url,
                width=image.width,
                height=image.height,
                ext=image_ext_from_url(image.url),
            )
            for image in post.images
        ]
        # 帖子里分享的视频只渲染封面 + 链接, **不下载**（那不是帖子本体）
        if post.video and post.video.cover_url:
            media.append(
                ImageRef(
                    url=post.video.cover_url,
                    thumb_url=post.video.cover_url,
                    ext=image_ext_from_url(post.video.cover_url),
                )
            )

        blocks = [post.text.strip()]
        if post.poll:
            blocks.append(YtbParse._poll_placeholder(0))
        if post.video:
            blocks.append(YtbParse._build_video_link(post.video))

        return MultimediaParseResult(
            content="\n\n".join(block for block in blocks if block),
            media=media,
            author_name=post.author_name,
            author_handle=post.author_handle,
            author_url=profile_url(Platform.YOUTUBE, post.author_handle),
            published_at=post.published_at,
            like_count=post.like_count,
            hashtags=post.hashtags,
            polls=[YtbParse._to_result_poll(post.poll)] if post.poll else [],
        )

    @staticmethod
    def _poll_placeholder(index: int) -> str:
        """投票 -> 正文里独立一行的占位符 ``{{poll:N}}``。

        **lib 只输出占位符**: 「投票 · 共 N 票」这类头部文案与单列表格由渲染层
        按用户语言拼装 (结构化数据在 ``result.polls`` 里)。
        """
        return f"{{{{poll:{index}}}}}"

    @staticmethod
    def _to_result_poll(poll: YoutubePostPoll) -> Poll:
        """帖子的投票 -> 结构化 ``Poll``。

        **每项的票数匿名拿不到**（页面只给 ``signinEndpoint``, 登录才有）⇒
        ``count`` 一律 None, 只有总票数; 因此也不给 ``voters``。
        """
        return Poll(
            options=[PollOption(text=choice) for choice in poll.choices],
            total_votes=poll.total_votes,
        )

    @staticmethod
    def _build_video_link(video: YoutubePostVideo) -> str:
        """帖子里分享的视频 -> 正文里的一行链接（封面另作一张图）。

        用 HTML ``<a>`` 而不是 markdown 链接: 正文与引用块两处都有效
        （markdown 链接在引用块里不解析, 同 bilibili 的标签处理）。
        """
        url = f"https://www.youtube.com/watch?v={video.video_id}"
        title = video.title or url
        return f'<a href="{url}">{html.escape(title)}</a>'


class YtbVideoParseResult(VideoParseResult):
    """视频 / 音乐解析结果 —— ``VideoRef.url`` 是 googlevideo **明文直链**。

    - **合一流**（``selected.audio_url is None``）: 交付项目下载器下单个文件。
    - **分离流**: 分别下视频与音频, 用 ffmpeg ``-c copy`` 合并成一个 mp4
      （视频带进度回调, 音频静默 —— 用户看到的最大文件是视频）。
    - **SABR**（``info.sabr`` 非空, 高清流没有明文直链）: 走
      ``provider_api/youtube_sabr.py`` 的 bun 工具把高清视频 + 音频拉到本地,
      再用 ffmpeg 合并 —— 直链下载器用不上（SABR 不是普通 HTTP 文件）。

    ``info``（player 元数据）与 ``selected``（选流）都保留为字段, 便于排查与测试。
    """

    def __init__(self, *, info: YoutubeVideo, selected: SelectedStreams, cookie: str | None = None):
        self.info = info
        self.selected = selected
        self.cookie = cookie
        """取视频用的 cookie 串（``k=v; k=v``）—— SABR 子进程要用同一个身份。
        只有走 SABR 时才需要; 明文直链下载自带签名, 用不上。"""
        super().__init__(
            title=info.title,
            content=info.description,
            author_name=info.author_name,
            # 作者主页与 handle 来自 WEB client 的 microformat（取流的 VISIONOS 不返回）；
            # 拿不到就退回 player 的 /channel/<id> 与空 handle（渲染层不显示空项）。
            author_handle=info.metadata.author_handle,
            author_url=info.metadata.author_url or info.author_url,
            published_at=info.metadata.published_at,
            like_count=info.metadata.like_count,
            view_count=info.view_count,
            video=VideoRef(
                url=selected.video_url,
                thumb_url=info.thumbnail,
                width=selected.width,
                height=selected.height,
                duration=selected.duration,
                ext=selected.ext,
            ),
        )

    async def _do_download(
        self,
        *,
        output_dir: Path,
        callback: ProgressCallback | None = None,
        callback_args: tuple = (),
        callback_kwargs: dict | None = None,
        proxy: str | None = None,
        headers: dict | None = None,
        connections: int = 4,
    ) -> DownloadResult:
        headers = {**self._download_headers(), **(headers or {})}
        # SABR: 高清流没有明文直链, 走 bun 工具拉流（不经过直链下载器）
        if self.info.sabr is not None:
            return await self._download_via_sabr(
                output_dir=Path(output_dir),
                callback=callback,
                callback_args=callback_args,
                callback_kwargs=callback_kwargs or {},
            )
        if self.selected.audio_url is None:
            return await super()._do_download(
                output_dir=output_dir,
                callback=callback,
                callback_args=callback_args,
                callback_kwargs=callback_kwargs,
                proxy=proxy,
                headers=headers,
                connections=connections,
            )
        return await self._download_and_mux(
            output_dir=Path(output_dir),
            callback=callback,
            callback_args=callback_args,
            callback_kwargs=callback_kwargs or {},
            proxy=proxy,
            headers=headers,
            connections=connections,
        )

    async def _download_via_sabr(
        self,
        *,
        output_dir: Path,
        callback: ProgressCallback | None,
        callback_args: tuple,
        callback_kwargs: dict,
    ) -> DownloadResult:
        """SABR 路径: bun 工具拉视频+音频到本地 → ffmpeg 合并 → 输出单文件。

        与直链路径的差别: 这里的字节来自 SABR 长连接（UMP 分帧）, 没有可下载的
        普通 URL —— 所以**不能**走 ``utils/downloader``, 必须先在工具里落盘。

        **输出格式跟素材走**: 选中的编码决定容器 —— vp9 是 webm（配 opus/webm），
        av1/avc 是 mp4（配 aac/m4a）。ffmpeg 合并后按容器定扩展名（webm 内容不能
        用 .mp4 撒谎：TG 与播放器都按扩展名识别）。

        失败**明确报错**（``DownloadError``）, 不静默降级到 360p:
        降级会让用户拿到低画质文件却以为解析成功。
        """
        sabr = self.info.sabr
        if sabr is None:  # _do_download 已按 sabr 分派, 这里只是给类型收窄
            raise DownloadError("内部错误: SABR 路径缺少素材")
        temp_dir = output_dir / f".{self.name}.sabr"
        temp_dir.mkdir(parents=True, exist_ok=True)
        progress = self._byte_progress(callback, callback_args, callback_kwargs) if callback else None

        # 预期总字节（用于进度）: 1080p 视频 + 音频的大致量级（取最高档 contentLength）
        expected = 0
        for item in sabr.adaptive_formats:
            length = int(item.get("contentLength") or 0)
            if length > expected and (item.get("mimeType") or "").startswith(("video/", "audio/")):
                expected = max(expected, length)
        expected = expected or 60 * 1024 * 1024

        try:
            result = await fetch_sabr_streams(
                self.info.video_id,
                abr_url=sabr.abr_url,
                ustreamer_config=sabr.ustreamer_config,
                adaptive_formats=list(sabr.adaptive_formats),
                cookie=self.cookie or "",
                output_dir=temp_dir,
                video_quality=sabr.video_quality,
                codec_priority=sabr.codec_priority,
                video_itag=sabr.video_itag,
                audio_itag=sabr.audio_itag,
                progress_callback=progress,
                progress_total=expected,
            )
            # 输出容器 = 工具实际选中的视频容器（webm/mp4）
            out_ext = result.video_path.suffix.lstrip(".").lower() or "mp4"
            output_path = output_dir / f"{self.name}.{out_ext}"
            await self._mux(result.video_path, result.audio_path, output_path)
        except SabrFetchError as e:
            raise DownloadError(f"SABR 取流失败: {e}") from e
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if callback:
            await callback(100, 100, "bytes", *callback_args, **callback_kwargs)

        return DownloadResult(
            VideoFile(
                path=str(output_path),
                width=self.selected.width,
                height=self.selected.height,
                duration=self.selected.duration,
            ),
            output_dir,
        )

    def _download_headers(self) -> dict[str, str]:
        """直链与 player 请求共用同一 client 的 UA。

        googlevideo 一般不看 UA（url 自带签名）, 但带上**实际发请求的那个 client 的 UA**
        最贴近正常播放器行为, 也更不容易被 CDN 挑刺。
        """
        return {"User-Agent": self.info.user_agent} if self.info.user_agent else {}

    async def _download_and_mux(
        self,
        *,
        output_dir: Path,
        callback: ProgressCallback | None,
        callback_args: tuple,
        callback_kwargs: dict,
        proxy: str | None,
        headers: dict,
        connections: int,
    ) -> DownloadResult:
        audio_url = self.selected.audio_url
        if audio_url is None:  # _do_download 已按 audio_url 分派, 这里只是给类型收窄
            raise DownloadError("内部错误: 分离流缺少音频直链")
        temp_dir = output_dir / f".{self.name}.mux"
        temp_dir.mkdir(parents=True, exist_ok=True)
        video_path = temp_dir / f"video.{self.selected.ext}"
        audio_path = temp_dir / "audio"
        progress = self._byte_progress(callback, callback_args, callback_kwargs) if callback else None
        try:
            await download(
                self.selected.video_url,
                video_path,
                headers=headers,
                proxy=proxy,
                progress=progress,
                progress_args=callback_args,
                progress_kwargs=callback_kwargs,
                connections=connections,
            )
            await download(audio_url, audio_path, headers=headers, proxy=proxy, connections=connections)

            output_path = output_dir / f"{self.name}.{self.selected.ext}"
            await self._mux(video_path, audio_path, output_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if callback:
            await callback(100, 100, "bytes", *callback_args, **callback_kwargs)

        return DownloadResult(
            VideoFile(
                path=str(output_path),
                width=self.selected.width,
                height=self.selected.height,
                duration=self.selected.duration,
            ),
            output_dir,
        )

    @staticmethod
    def _byte_progress(
        callback: ProgressCallback,
        callback_args: tuple,
        callback_kwargs: dict,
    ) -> ProgressCallback:
        """把下载器的 ``(current, total)`` 回调适配成 ``(current, total, "bytes")``。"""

        async def _progress(current: int, total: int, *args, **kwargs) -> None:
            await callback(current, total, "bytes", *args, **kwargs)

        return _progress

    @staticmethod
    async def _mux(video_path: Path, audio_path: Path, output_path: Path) -> None:
        """ffmpeg ``-c copy`` 合并（不转码, 秒级完成）。失败明确报错, 不产出空文件。

        ``-movflags +faststart`` 只对 mp4/mov 有效 —— webm 输出加上它 ffmpeg 会报错，
        所以按输出扩展名决定要不要带。
        """
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-c",
            "copy",
        ]
        if output_path.suffix.lower() in (".mp4", ".m4v", ".mov"):
            cmd += ["-movflags", "+faststart"]
        cmd += ["-y", str(output_path)]
        proc = await asyncio.create_subprocess_exec(  # pylint: disable=no-member  # asyncio.subprocess 存在, pylint 解析不到
            *cmd,
            stdout=asyncio.subprocess.PIPE,  # pylint: disable=no-member
            stderr=asyncio.subprocess.PIPE,  # pylint: disable=no-member
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
            detail = (stderr or b"").decode("utf-8", "replace").strip()[-500:]
            raise DownloadError(f"ffmpeg 合并音视频失败: {detail or f'退出码 {proc.returncode}'}")
        logger.info(f"音视频合并完成: {output_path.name} ({output_path.stat().st_size / 1048576:.2f}MB)")


__all__ = ["YtbParse"]
