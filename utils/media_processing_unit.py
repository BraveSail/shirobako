"""媒体处理器 — 将图片/视频转换为 Telegram 兼容格式"""

import asyncio
import json
import math
import mimetypes
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from haishoku.haishoku import Haishoku
from loguru import logger
from PIL import Image, ImageOps
from PIL.Image import Resampling

from utils.helpers import run_cmd

#: Pillow 的**格式名** → 常规文件后缀。降采样时输出后缀要跟真实格式一致
#: （``.jfif``/``.jpeg`` 这类别名统一成常规写法；Pillow 的 ``format`` 是这些大写名）。
#: ⚠️ 这是「Pillow 怎么称呼这个格式」, 不是「Telegram 收不收」—— 后者见 ``_TG_PHOTO_SUFFIX``。
_PIL_FORMAT_SUFFIX = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
    "GIF": "gif",
    "BMP": "bmp",
    "TIFF": "tiff",
    "ICO": "ico",
    "AVIF": "avif",
    "HEIF": "heif",
    "JPEG2000": "jp2",
    "MPO": "mpo",
    "PPM": "ppm",
}

#: **内容格式 → 出口后缀**: 既能让 Telegram 直接解码、又在它的后缀白名单里的组合。
#: 依据（2026-10-14 容器内 pyrogram 走生产同款 ``messages.UploadMedia`` 实测）：
#:
#: - 服务端**按扩展名判类型**，白名单只有 ``.jpg/.jpeg/.jfif/.png/.gif/.bmp``；
#:   ``.webp/.tiff/.tif/.ico/.ppm/.jp2`` 一律 ``400 PHOTO_EXT_INVALID``。
#: - 内容层宽松：JPEG / PNG / GIF / BMP / **WEBP** 的内容配白名单后缀都能发（服务端自己解码）；
#:   TIFF 内容是 ``400 IMAGE_PROCESS_FAILED`` ⇒ 必须本地重编码（走 ``_img2jpg``）。
#: - 同一份字节只改文件名即可从"被拒"变"通过"（生产两个真实文件实测如此）。
#:
#: ⇒ **WEBP 内容不需要重编码**，只要出口叫 ``.jpg`` 就能直接发（零画质损失、零 CPU）。
_TG_PHOTO_SUFFIX = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "BMP": ".bmp",
    "WEBP": ".jpg",  # 内容仍是 webp, 只是名字落在服务端白名单里
}

#: Telegram 愿意**直接解码**的图片格式（= 上面那张表的 key，不必先本地重编码）。
#: 表外的格式（TIFF / ICO / PPM / JPEG2000…）本地转成 JPEG 后再发。
_TG_PHOTO_FORMATS = frozenset(_TG_PHOTO_SUFFIX)


@dataclass
class MediaProcessResult:
    """统一处理结果"""

    output_paths: list[Path]
    temp_dir: Path | None = None


class MediaProcessingUnit:
    """媒体处理器，将媒体转换为 Telegram 兼容的格式

    Telegram 限制：
    - 图片宽高比 / 高宽比不能超过 20:1
    - 单次最多发送 10 张图片
    - 单个视频最大 2000 MB

    用法：
        mpu = MediaProcessingUnit(output_dir=Path("./output"))
        result = await mpu.process("media.mp4")
    """

    TG_MAX_VIDEO_SIZE = 2000 * 1000**2

    def __init__(
        self,
        output_dir: str | Path,
        segment_height: int = 1400,
        medium_threshold: int = 2,
        overlap: int = 100,
        logger: Callable = logger.info,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.segment_height = segment_height
        self.medium_threshold = medium_threshold
        self.overlap = overlap
        self.logger = logger

    # ------------------------------------------------------------------ #
    #  公共入口
    # ------------------------------------------------------------------ #

    async def process(self, file_path: str | Path) -> MediaProcessResult:
        media_type = self.get_media_type_by_mime(file_path)
        self.logger(f"开始处理媒体: path={file_path}, type={media_type}")
        if media_type == "image":
            return await self.process_image(Path(file_path))
        elif media_type == "video":
            return await self.process_video(Path(file_path))
        else:
            raise ValueError(f"Unsupported media type: {file_path}")

    # ------------------------------------------------------------------ #
    #  图片处理
    # ------------------------------------------------------------------ #

    async def process_image(self, file_path: Path) -> MediaProcessResult:
        image_format, is_broken, image_mode = await self._detect_image_format(file_path)
        if image_format == "GIF":
            # 动图/多帧图不重编码（转 JPEG 会丢帧），只把后缀对齐到服务端认的名字
            self.logger("图片为 GIF 格式，无需处理")
            return MediaProcessResult(output_paths=[self._align_tg_photo_ext(file_path)])
        needs_convert = image_format not in _TG_PHOTO_FORMATS
        source = file_path
        intermediates: list[Path] = []  # 统一收集中间文件

        try:
            if needs_convert or is_broken:
                self.logger(f"图片需转换: format={image_format}, mode={image_mode}, broken={is_broken}")
                source = await self._img2jpg(file_path)
                intermediates.append(source)

            if result := await asyncio.to_thread(self._adapt_image, source):
                outputs = [await asyncio.to_thread(self._align_tg_photo_ext, p) for p in result.output_paths]
                return MediaProcessResult(output_paths=outputs, temp_dir=result.temp_dir)

            # _adapt_image 无需处理，尝试 downscale
            if downscaled := await asyncio.to_thread(self._downscale_image, source):
                intermediates.append(downscaled)
                source = downscaled

            intermediates = [p for p in intermediates if p != source]
            source = await asyncio.to_thread(self._align_tg_photo_ext, source)
            return MediaProcessResult(output_paths=[source])
        finally:
            for p in intermediates:
                if p.exists():
                    self.logger(f"删除中间文件: {p}")
                    os.remove(p)

    def _align_tg_photo_ext(self, file_path: Path) -> Path:
        """把产物后缀对齐到 Telegram 能接受的**照片后缀**，返回（可能改名后的）路径。

        实测规则（2026-10-14，容器内 pyrogram 走生产同款 ``messages.UploadMedia``）：

        - **只看扩展名**：``.jpg/.jpeg/.jfif/.png/.gif/.bmp`` 接受；
          ``.webp/.tiff/.tif/.ico/.ppm/.jp2`` 一律 ``400 PHOTO_EXT_INVALID``。
        - **内容层宽松**：JPEG / PNG / GIF / BMP / WEBP 的内容配白名单后缀都能发
          （服务端自己解码重编码）；TIFF 内容是 ``400 IMAGE_PROCESS_FAILED``，必须重编码。
        - 同一份字节只改文件名即可从"被拒"变"通过"（生产那两个真实文件实测如此）。

        所以这里**只改名、不重编码**：内容一字不动，画质与体积都不变。
        下载文件名的后缀是按 URL 猜的（``ImageRef.ext`` ← ``image_ext_from_url``），
        URL 与实际字节不一致时（如小黑盒 ``.../xxx.webp?…/format/jpg/…``：URL 说 webp、
        CDN 给 JPEG）整条消息会被 Telegram 拒收，媒体一条都发不出去。

        文件打不开（Pillow 不认）时原样返回 —— 那类留给 ``_img2jpg`` 的 ffmpeg 兜底，
        不在这里猜。
        """
        try:
            with Image.open(file_path) as img:
                actual_format = (img.format or "").upper()
        except OSError:
            return file_path
        target_suffix = _TG_PHOTO_SUFFIX.get(actual_format)
        if not target_suffix:
            return file_path
        if file_path.suffix.lower() == target_suffix:
            return file_path
        new_path = file_path.with_suffix(target_suffix)
        if new_path.exists():
            self.logger(f"后缀对齐目标已存在, 保持原样: {file_path} -> {new_path}")
            return file_path
        file_path.rename(new_path)
        self.logger(f"后缀与真实格式不符, 改名: {file_path.name} -> {new_path.name} (format={actual_format})")
        return new_path

    @staticmethod
    async def _detect_image_format(file_path: Path) -> tuple[str, bool, str]:
        try:
            with Image.open(file_path) as img:
                return (img.format or "").upper(), False, img.mode
        except OSError:
            return await MediaProcessingUnit._probe_image_format(file_path), True, ""

    @staticmethod
    async def _probe_image_format(file_path: Path) -> str:
        codec = await run_cmd(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        )
        return {"mjpeg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}.get(
            codec.strip().lower(), codec.strip().upper()
        )

    def _adapt_image(self, file_path: Path) -> MediaProcessResult | None:
        """分析图片尺寸并做填充 / 切割，返回 None 表示无需处理"""
        with Image.open(file_path) as img:
            w, h = img.width, img.height

        wh_ratio = w / h
        hw_ratio = h / w
        self.logger(f"图片尺寸: {w}x{h}, wh_ratio={wh_ratio:.2f}, hw_ratio={hw_ratio:.2f}")

        if w >= h:
            # 横图
            if wh_ratio <= 20:
                self.logger("横图比例正常，跳过处理")
                return None
            self.logger("横图比例超限，需要填充")
            padding = self._calc_padding_horizontal(w, h)
            with Image.open(file_path) as img:
                return self._pad_image(file_path, img, padding)
        else:
            # 竖图
            if hw_ratio <= 5 or (w < 200 and hw_ratio < 20):
                self.logger("竖图比例正常，跳过处理")
                return None
            if w < 200 and hw_ratio > 20:
                self.logger("窄竖图比例超限，需要填充")
                padding = self._calc_padding_vertical(w, h)
                with Image.open(file_path) as img:
                    return self._pad_image(file_path, img, padding)
            # 长图切割
            segments = h // self.segment_height
            seg_h = h // 2 if segments < self.medium_threshold else self.segment_height
            self.logger(f"长图切割: segments={segments}, seg_h={seg_h}")
            return self._split_image(file_path, seg_h)

    async def _img2jpg(self, file_path: Path) -> Path:
        output = self.output_dir / file_path.with_suffix(".jpg").name
        try:
            with Image.open(file_path) as pil_img:
                img = pil_img.convert("RGB") if pil_img.mode != "RGB" else pil_img
                img.save(output, format="JPEG")
        except OSError as e:
            self.logger(f"Pillow 转换失败，尝试 ffmpeg: {e}")
            await run_cmd(
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(file_path),
                "-frames:v",
                "1",
                "-q:v",
                "1",
                "-y",
                str(output),
                timeout=60,
            )
            if not output.exists() or output.stat().st_size == 0:
                raise
        self.logger(f"图片转换完成: {output}")
        return output

    def _downscale_image(self, file_path: Path, max_side: int = 2560) -> Path | None:
        """若图片任一边超过 max_side，等比缩放至长边为 max_side，返回新文件路径；无需缩放返回 None

        **保存格式跟真实格式走，不跟文件后缀走**：下载下来的图偶尔"后缀撒谎"
        （twitter 的 PNG 被命名成 .jpg），而 ``save()`` 不指定 format 时 Pillow 会用**后缀**
        推断编码器 —— 调色板图（mode ``P``）无法写成 JPEG，于是抛
        ``cannot write mode P as JPEG``，整条媒体处理就失败了。
        所以这里显式给 ``format`` 并让输出后缀与真实格式一致。
        """
        with Image.open(file_path) as img:
            w, h = img.size
            if max(w, h) <= max_side:
                return None
            scale = max_side / max(w, h)
            new_w, new_h = int(w * scale), int(h * scale)
            self.logger(f"图片长边超限({max(w, h)}px > {max_side}px)，缩放: {w}x{h} -> {new_w}x{new_h}")
            resized = img.resize((new_w, new_h), Resampling.LANCZOS)
            actual_format = (img.format or "").upper()
            if actual_format:
                suffix = f".{_PIL_FORMAT_SUFFIX.get(actual_format, actual_format.lower())}"
                out_path = self.output_dir / f"{file_path.stem}_downscaled{suffix}"
                resized.save(out_path, format=actual_format)
            else:
                # 拿不到真实格式（极少见）时保持旧行为: 用原后缀、由 Pillow 自行推断
                out_path = self.output_dir / f"{file_path.stem}_downscaled{file_path.suffix}"
                resized.save(out_path)
        return out_path

    # -- 图片辅助 --------------------------------------------------------- #

    @staticmethod
    def _calc_padding_horizontal(w: int, h: int) -> tuple[int, int, int, int]:
        h_padding = w // 20 - h // 2
        return 0, h_padding, 0, h_padding

    @staticmethod
    def _calc_padding_vertical(w: int, h: int) -> tuple[int, int, int, int]:
        w_padding = h // 20 - w // 2
        return w_padding, 0, w_padding, 0

    @staticmethod
    def _get_dominant_color(file_path: Path) -> tuple[int, ...]:
        haishoku = Haishoku.loadHaishoku(str(file_path))
        # palette 是该库运行期挂上的属性 (形如 [(count, (r,g,b)), ...]), pylint 解析不到
        return tuple(int(v * 0.8) for v in haishoku.palette[0][1])  # pylint: disable=unsubscriptable-object

    def _pad_image(
        self,
        file_path: Path,
        img: Image.Image,
        padding: tuple[int, int, int, int],
    ) -> MediaProcessResult:
        fill_color = self._get_dominant_color(file_path)
        padded = ImageOps.expand(img, padding, fill=fill_color)
        out_path = self.output_dir / f"padded_{time.time_ns()}.png"
        padded.save(out_path)
        self.logger(f"填充完成: padding={padding}, color={fill_color}, output={out_path}")
        return MediaProcessResult(output_paths=[out_path])

    def _split_image(self, file_path: Path, segment_height: int) -> MediaProcessResult:
        temp_dir = self.output_dir / f"split_{time.time_ns()}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        segments = self._do_split(file_path, temp_dir, segment_height)
        self.logger(f"图片切割完成: {len(segments)} 段, output_dir={temp_dir}")
        return MediaProcessResult(output_paths=segments, temp_dir=temp_dir)

    def _do_split(
        self,
        input_path: Path,
        output_dir: Path,
        segment_height: int,
    ) -> list[Path]:
        with Image.open(input_path) as img:
            width, height = img.size
            num_segments = math.ceil(height / segment_height)
            self.logger(f"切割参数: size={width}x{height}, segment_h={segment_height}, num={num_segments}")
            result: list[Path] = []
            for i in range(num_segments):
                top = i * segment_height - (self.overlap if i != 0 else 0)
                bottom = min((i + 1) * segment_height, height)
                segment = img.crop((0, top, width, bottom))
                out_path = output_dir / f"segment_{i + 1:03d}.png"
                segment.save(out_path)
                result.append(out_path)
        return result

    # ------------------------------------------------------------------ #
    #  视频处理
    # ------------------------------------------------------------------ #

    async def process_video(self, file_path: Path) -> MediaProcessResult:
        container = await self.get_container(file_path)
        self.logger(f"视频容器: container={container}, path={file_path}")

        converted: Path | None = None
        if "mp4" not in container:
            self.logger("容器非 mp4，开始重新封装")
            converted = await self.remux_to_mp4(file_path)
            self.logger(f"封装完成: {converted}")

        source = converted or file_path
        video_size = source.stat().st_size
        self.logger(f"视频大小: {video_size / 1000 / 1000:.1f} MB ({video_size / 1024 / 1024:.1f} MiB)")

        if video_size > self.TG_MAX_VIDEO_SIZE:
            self.logger(f"视频超过 {self.TG_MAX_VIDEO_SIZE / 1000 / 1000:.0f} MB 限制，开始分割")
            output_paths, output_dir = await self.split_video(source, self.output_dir, self.TG_MAX_VIDEO_SIZE)
            if converted:
                os.remove(converted)
            return MediaProcessResult(output_paths=output_paths, temp_dir=output_dir)

        return MediaProcessResult(output_paths=[source])

    @staticmethod
    async def get_video_codec(file_path: Path) -> str:
        out = await run_cmd(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        )
        return out.strip().lower() if out else ""

    @staticmethod
    async def get_container(file_path: Path) -> str:
        result = await run_cmd(
            "ffprobe", "-v", "error", "-show_entries", "format=format_name", "-of", "json", str(file_path)
        )
        r: dict[str, dict[str, str]] = json.loads(result)
        if not (f := r.get("format", {})):
            raise ValueError(f"ffprobe failed to get container: {result}")
        return f["format_name"]

    @staticmethod
    async def get_duration(file_path: Path) -> float:
        out = await run_cmd(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        )
        return float(out.strip()) if out else 0.0

    async def remux_to_mp4(self, file_path: Path) -> Path:
        out = self.output_dir / (file_path.stem + "_remux" + ".mp4")
        cmd = ["ffmpeg", "-i", str(file_path), "-c", "copy", "-movflags", "+faststart", "-y", str(out)]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return out

    async def ensure_h264(self, file_path: Path) -> Path:
        out = self.output_dir / (file_path.stem + "_h264" + file_path.suffix)
        duration = await self.get_duration(file_path)
        height = await self._get_video_height(file_path)

        cmd = self._build_sw_transcode_cmd(file_path, out, duration, height)

        self.logger(f"h264 转码: {file_path.name} -> {out.name}, duration={duration:.0f}s, encoder=SW:libx264")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        if out.exists() and out.stat().st_size > 0:
            self.logger(f"h264 转码成功: size={out.stat().st_size / 1024 / 1024:.1f}MB")
            return out

        self.logger(f"h264 转码失败，返回原文件: {file_path}")
        return file_path

    @staticmethod
    async def _get_video_height(file_path: Path) -> int:
        out = await run_cmd(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=height",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        )
        return int(out.strip()) if out and out.strip().isdigit() else 0

    def _build_sw_transcode_cmd(self, file_path: Path, out: Path, duration: float, height: int) -> list[str]:
        if duration <= 30:
            preset, crf = "slow", "18"
        elif duration <= 60:
            preset, crf = "medium", "20"
        elif duration <= 600:
            preset, crf = "fast", "23"
        elif duration <= 1800:
            preset, crf = "veryfast", "26"
        else:
            preset, crf = "ultrafast", "28"

        scale = ["-vf", "scale=-2:720"] if duration > 1800 and height > 720 else []
        self.logger(f"SW 转码策略: preset={preset}, crf={crf}, scale={'720p' if scale else 'original'}")

        return [
            "ffmpeg",
            "-i",
            str(file_path),
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            crf,
            *scale,
            "-c:a",
            "aac",
            "-y",
            str(out),
        ]

    async def split_video(
        self,
        file_path: Path,
        output_dir: Path,
        size_limit: int = TG_MAX_VIDEO_SIZE,
        ffmpeg_args: list[str] | None = None,
        keep_sec: float = 1.0,
    ) -> tuple[list[Path], Path]:
        if ffmpeg_args is None:
            ffmpeg_args = ["-c", "copy"]

        base = file_path.stem
        split_dir = output_dir / f"{base}_split"
        split_dir.mkdir(parents=True, exist_ok=True)
        ext = file_path.suffix.lstrip(".")
        total_duration = int(await self.get_duration(file_path))
        self.logger(
            f"视频分割: duration={total_duration}s, "
            f"size_limit={size_limit / 1000 / 1000:.0f} MB ({size_limit / 1024 / 1024:.1f} MiB)"
        )

        cur, part, output_paths = 0, 1, []
        while cur < total_duration:
            out_file = split_dir / f"{base}_part_{part:03d}.{ext}"
            output_paths.append(out_file)
            cmd = [
                "ffmpeg",
                "-ss",
                str(cur),
                "-i",
                str(file_path),
                "-fs",
                str(size_limit),
                *ffmpeg_args,
                "-y",
                str(out_file),
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

            new_dur = int(await self.get_duration(out_file))
            self.logger(f"分割 part {part}: offset={cur}s, duration={new_dur}s, file={out_file}")
            if new_dur <= 0:
                break
            cur += new_dur
            if cur < total_duration:
                cur = max(cur - int(keep_sec), 0)
            part += 1

        self.logger(f"视频分割完成: {len(output_paths)} 段")
        return output_paths, split_dir

    # ------------------------------------------------------------------ #
    #  工具方法
    # ------------------------------------------------------------------ #

    @staticmethod
    def get_media_type_by_mime(file_path: str | Path) -> str:
        mime, _ = mimetypes.guess_type(str(file_path))
        if mime:
            if mime.startswith("image/"):
                return "image"
            if mime.startswith("video/"):
                return "video"
        return "unknown"


async def main() -> None:
    mpu = MediaProcessingUnit(
        output_dir=Path(r"/home/mio/Projects/personal/parse_hub_bot/downloads/与辉同行25年6月13日-一")
    )
    result = await mpu.process(
        r"/home/mio/Projects/personal/parse_hub_bot/downloads/与辉同行25年6月13日-一/与辉同行25年6月13日-一.mp4"
    )
    print(result.output_paths)


if __name__ == "__main__":
    asyncio.run(main())
