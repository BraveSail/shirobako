"""媒体处理: 降采样不能按**文件后缀**猜保存格式。

背景（2026-10-08, 用户报「▎媒体处理错误: cannot write mode P as JPEG」, 推文
``kon_kokine/status/2107756845704360201``）:

那张图是 **PNG 调色板图（mode ``P``）**, 但下载时被命名成 ``.jpg``
（``ImageRef.ext`` 默认 ``jpg``, twitter 解析器没传 ext）。``_downscale_image`` 里
``resized.save(out_path)`` 不指定 format ⇒ Pillow 按**后缀**选编码器 ⇒ JPEG 编不了 ``P``
⇒ 抛错, 整条媒体处理失败（用户只看到"媒体处理错误"）。

判据: **保存格式跟真实格式走** —— 后缀撒谎的 P 模式 PNG 降采样后仍是 PNG, 输出后缀也是 ``.png``。
"""

import asyncio
from pathlib import Path

from PIL import Image

from utils.media_processing_unit import MediaProcessingUnit

#: 长边超限才会触发降采样（默认 2560）; 这个尺寸比例正常, 不会进填充/切割分支
_OVERSIZED = (2700, 1200)


def _unit(tmp_path: Path) -> MediaProcessingUnit:
    return MediaProcessingUnit(tmp_path / "out", segment_height=1920, logger=lambda _m: None)


def _write_image(path: Path, fmt: str, mode: str, size: tuple[int, int]) -> Path:
    Image.new(mode, size).save(path, format=fmt)
    return path


# ---------------------------------------------------------------- 核心回归


def test_palette_png_with_a_lying_extension_still_downscales(tmp_path):
    """**核心回归**: PNG 内容 + ``.jpg`` 后缀 + mode ``P`` 不能炸。

    旧实现按后缀存 JPEG, 直接 ``OSError: cannot write mode P as JPEG``。
    """
    unit = _unit(tmp_path)
    src = _write_image(tmp_path / "001_欺骗.jpg", "PNG", "P", _OVERSIZED)

    out = unit._downscale_image(src)  # noqa: SLF001 — 直接钉住这一层的行为

    assert out is not None
    assert out.suffix == ".png", "输出后缀必须跟随真实格式, 不能继续撒谎"
    with Image.open(out) as img:
        assert img.format == "PNG"
        assert img.mode == "P", "调色板模式要原样保留, 不该被转成 JPEG"
        assert max(img.size) == 2560


def test_real_jpeg_downscales_to_jpeg_as_before(tmp_path):
    """真 JPEG 走原路径: 后缀与格式都不变（这次改动对它是零变化）。"""
    unit = _unit(tmp_path)
    src = _write_image(tmp_path / "photo.jpg", "JPEG", "RGB", _OVERSIZED)

    out = unit._downscale_image(src)  # noqa: SLF001

    assert out is not None
    assert out.suffix == ".jpg"
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert max(img.size) == 2560


def test_rgba_png_downscales_as_png(tmp_path):
    """带透明的 PNG 同理: 透明通道不能因为"按后缀存"而丢掉。"""
    unit = _unit(tmp_path)
    src = _write_image(tmp_path / "alpha.png", "PNG", "RGBA", _OVERSIZED)

    out = unit._downscale_image(src)  # noqa: SLF001

    assert out is not None
    with Image.open(out) as img:
        assert img.format == "PNG"
        assert img.mode == "RGBA"


def test_webp_keeps_its_own_format(tmp_path):
    unit = _unit(tmp_path)
    src = _write_image(tmp_path / "pic.webp", "WEBP", "RGB", _OVERSIZED)

    out = unit._downscale_image(src)  # noqa: SLF001

    assert out is not None
    assert out.suffix == ".webp"
    with Image.open(out) as img:
        assert img.format == "WEBP"


def test_small_image_is_left_alone(tmp_path):
    """长边不超限 -> 不动它（返回 None, 不产生任何文件）。"""
    unit = _unit(tmp_path)
    src = _write_image(tmp_path / "small.png", "PNG", "P", (100, 80))

    assert unit._downscale_image(src) is None  # noqa: SLF001
    assert not list((tmp_path / "out").glob("*"))


# ---------------------------------------------------------------- 全链路


def test_process_image_handles_a_lying_palette_png_end_to_end(tmp_path):
    """端到端: 比例正常的"后缀撒谎"调色板图, 走 ``process_image`` 不再失败。

    （尺寸选 2700x1200 —— 横图比例 2.25 < 20, 不会进填充/切割, 只触发降采样。）
    """
    import asyncio

    unit = _unit(tmp_path)
    src = _write_image(tmp_path / "001_欺骗.jpg", "PNG", "P", _OVERSIZED)

    result = asyncio.run(unit.process_image(src))

    assert len(result.output_paths) == 1
    out = result.output_paths[0]
    with Image.open(out) as img:
        assert img.format == "PNG"
        assert max(img.size) == 2560


# ---------------------------------------------------------------- 后缀 vs 真实内容


def test_jpeg_content_saved_as_webp_gets_real_extension(tmp_path):
    """**核心回归**（2026-10-14 生产报 `PHOTO_EXT_INVALID`）: 内容 JPEG + 名字 ``.webp``。

    小黑盒的图 URL 是 ``.../xxx.webp?imageMogr2/.../format/jpg/...`` —— path 后缀是 ``webp``,
    CDN 却按 query 转成了 **JPEG**。``ImageRef.ext`` 取 path 后缀 ⇒ 文件名 ``.webp``,
    而 ``process_image`` 看到 Pillow 的 ``JPEG`` 就跳过转换、原样把 ``.webp`` 交出去,
    Telegram 按**扩展名**判类型 ⇒ ``400 PHOTO_EXT_INVALID``, 整条消息发不出去。

    判据: 产物后缀跟随**真实格式**（且是改名, 不重编码 —— 字节必须一字不差）。
    """
    unit = _unit(tmp_path)
    src = _write_image(tmp_path / "001_小黑盒.webp", "JPEG", "RGB", (850, 1090))
    before = src.read_bytes()

    result = asyncio.run(unit.process_image(src))

    assert len(result.output_paths) == 1
    out = result.output_paths[0]
    assert out.suffix == ".jpg", "后缀必须跟真实内容走, 否则 Telegram 按扩展名判类型会拒收"
    with Image.open(out) as img:
        assert img.format == "JPEG"
    assert out.read_bytes() == before, "只是改名, 不该重新编码（画质/体积不能变）"


def test_png_content_saved_as_jpg_gets_real_extension(tmp_path):
    """对称情形: 内容 PNG + 名字 ``.jpg``（小图, 不触发降采样/其它处理）。"""
    unit = _unit(tmp_path)
    src = _write_image(tmp_path / "002_撒谎.jpg", "PNG", "RGB", (300, 200))

    result = asyncio.run(unit.process_image(src))

    out = result.output_paths[0]
    assert out.suffix == ".png"
    with Image.open(out) as img:
        assert img.format == "PNG"


def test_matching_extension_keeps_the_same_file(tmp_path):
    """后缀已经与真实格式一致 -> 原路径返回, 不产生任何新文件。"""
    unit = _unit(tmp_path)
    src = _write_image(tmp_path / "003_normal.jpg", "JPEG", "RGB", (300, 200))

    result = asyncio.run(unit.process_image(src))

    assert result.output_paths == [src]
    assert list((tmp_path / "out").glob("*")) == []


# ---------------------------------------------------------------- "能发就不转码"


def test_webp_is_sent_as_is_only_renamed(tmp_path):
    """**核心回归**: WEBP 内容**不重编码**, 只把后缀换到 Telegram 的白名单里。

    旧实现 ``needs_convert = format not in {PNG, JPEG}`` 会把 WEBP 整张转成 JPEG
    —— 实测服务端本就能直接解码 WEBP 内容, 只要后缀不是 ``.webp``
    （白名单 ``.jpg/.jpeg/.jfif/.png/.gif/.bmp``; ``.webp`` → ``400 PHOTO_EXT_INVALID``）。
    所以判据: 产物后缀 ``.jpg``、内容仍是 WEBP（零画质损失、零 CPU）。
    """
    unit = _unit(tmp_path)
    src = _write_image(tmp_path / "004_pic.webp", "WEBP", "RGB", (400, 300))
    before = src.read_bytes()

    result = asyncio.run(unit.process_image(src))

    out = result.output_paths[0]
    assert out.suffix == ".jpg", "WEBP 要改名成白名单后缀, 而不是重编码"
    with Image.open(out) as img:
        assert img.format == "WEBP", "不该被重编码 —— 服务端自己会解码"
    assert out.read_bytes() == before, "字节必须一字不差"


def test_bmp_is_sent_as_is(tmp_path):
    """BMP 同理: 内容可直接发, 后缀 ``.bmp`` 在白名单里, 完全不动。"""
    unit = _unit(tmp_path)
    src = _write_image(tmp_path / "005_pic.bmp", "BMP", "RGB", (400, 300))

    result = asyncio.run(unit.process_image(src))

    out = result.output_paths[0]
    assert out == src
    with Image.open(out) as img:
        assert img.format == "BMP"


def test_tiff_is_still_transcoded(tmp_path):
    """表外格式（TIFF）仍必须本地重编码 —— 实测服务端报 ``IMAGE_PROCESS_FAILED``。"""
    unit = _unit(tmp_path)
    src = _write_image(tmp_path / "006_pic.tiff", "TIFF", "RGB", (400, 300))

    result = asyncio.run(unit.process_image(src))

    out = result.output_paths[0]
    assert out.suffix == ".jpg"
    with Image.open(out) as img:
        assert img.format == "JPEG"


def test_gif_keeps_frames_and_only_aligns_ext(tmp_path):
    """GIF 走早期返回（不重编码，转 JPEG 会丢帧），后缀对齐到白名单。"""
    unit = _unit(tmp_path)
    src = tmp_path / "007_anim.gif"
    Image.new("P", (300, 200)).save(src, format="GIF")

    result = asyncio.run(unit.process_image(src))

    out = result.output_paths[0]
    assert out == src
    with Image.open(out) as img:
        assert img.format == "GIF"


def test_gif_named_jpg_gets_renamed_to_gif(tmp_path):
    """GIF 内容 + ``.jpg`` 名 -> 改回 ``.gif``（内容里是多帧，不能当静态图发）。"""
    unit = _unit(tmp_path)
    src = tmp_path / "008_liar.jpg"
    Image.new("P", (300, 200)).save(src, format="GIF")

    result = asyncio.run(unit.process_image(src))

    assert result.output_paths[0].suffix == ".gif"
