"""SABR 选画质档位：用 qualityLabel，**不能用 height**。

## 真实故障（2026-11-14，用户报 Shorts 抓不到 1080）

`https://www.youtube.com/shorts/d33zJn8-bEQ` 下载出来是 **608×1080**，而素材里
明明有 **1080×1920**。用户问「怎么抓不到 1080？」

**根因**：选流用 ``height <= maxHeight(1080)`` 过滤档位。竖屏视频里

| 分辨率 | height | qualityLabel | 档位 |
| --- | --- | --- | --- |
| 1080×1920 | **1920** | 1080p | 真 1080p ← 被过滤掉了 |
| 608×1080 | **1080** | 480p | 低档 ← 被留下了 |

横屏视频 height 恰好等于档位值（1920×1080 的 height 就是 1080），所以这个 bug
**只在竖屏（Shorts）暴露**，普通视频一直是对的。

## 判据

用 ``qualityLabel`` 的前导数字当档位（``1080p``→1080、``2160p60``→2160）。
实测它与 ``qualityOrdinal`` 在最高档上等价（都选中 1080×1920），但 label 更直观 ——
用户说的「1080p」就是这个词。``608P`` 在 label 里同属 ``480p`` 档，那是 YouTube
自己的档位命名，不是错误。

⚠️ 别再用 height 判断档位；也别忘了 ``maxHeight`` 的语义是**档位上限**而不是像素高度。
"""

from __future__ import annotations

from parsehub.provider_api.youtube_video import _sabr_tier, _sabr_best_quality


class _FakeSabr:
    def __init__(self, formats):
        self.adaptive_formats = formats


def _fmt(itag: int, w: int, h: int, label: str, mime: str = 'video/mp4; codecs="av01.0.08M.08"') -> dict:
    return {"itag": itag, "width": w, "height": h, "qualityLabel": label, "mimeType": mime}


#: 真实响应里的关键几条（shorts d33zJn8-bEQ）
SHORTS_FORMATS = [
    _fmt(137, 1080, 1920, "1080p", 'video/mp4; codecs="avc1.640028"'),
    _fmt(248, 1080, 1920, "1080p", 'video/webm; codecs="vp9"'),
    _fmt(399, 1080, 1920, "1080p"),
    _fmt(136, 720, 1280, "720p", 'video/mp4; codecs="avc1.4D401F"'),
    _fmt(780, 608, 1080, "480p", 'video/webm; codecs="vp9"'),
    _fmt(788, 608, 1080, "480p"),
    _fmt(135, 480, 854, "480p", 'video/mp4; codecs="avc1.4D401F"'),
    _fmt(160, 144, 256, "144p", 'video/mp4; codecs="avc1.4D400C"'),
]


class TestSabrTier:
    def test_parses_leading_number(self) -> None:
        assert _sabr_tier({"qualityLabel": "1080p"}) == 1080
        assert _sabr_tier({"qualityLabel": "720p"}) == 720
        assert _sabr_tier({"qualityLabel": "144p"}) == 144

    def test_handles_fps_suffix(self) -> None:
        assert _sabr_tier({"qualityLabel": "2160p60"}) == 2160
        assert _sabr_tier({"qualityLabel": "1080p60"}) == 1080

    def test_missing_label_is_zero(self) -> None:
        assert _sabr_tier({}) == 0
        assert _sabr_tier({"qualityLabel": None}) == 0
        assert _sabr_tier({"qualityLabel": ""}) == 0

    def test_regression_608p_is_not_1080p(self) -> None:
        """608×1080 的 height 是 1080，但档位是 480p —— 别再按 height 判。"""
        assert _sabr_tier(_fmt(780, 608, 1080, "480p")) == 480


class TestBestQualityOnShorts:
    def test_picks_1080x1920_not_608x1080(self) -> None:
        """★ 核心回归：必须选 1080×1920，不能因为 height>1080 就跳过它。"""
        best = _sabr_best_quality(_FakeSabr(SHORTS_FORMATS))
        assert best == (1080, 1920), f"选到了 {best}，应该是 (1080, 1920)"

    def test_respects_max_height_as_tier(self) -> None:
        """max_height 是**档位上限**：1080 允许 1080p 档（哪怕 height=1920）。"""
        best = _sabr_best_quality(_FakeSabr(SHORTS_FORMATS), max_height=1080)
        assert best == (1080, 1920)

    def test_max_height_720_excludes_1080_tier(self) -> None:
        best = _sabr_best_quality(_FakeSabr(SHORTS_FORMATS), max_height=720)
        assert best == (720, 1280)

    def test_max_height_zero_means_no_limit(self) -> None:
        best = _sabr_best_quality(_FakeSabr(SHORTS_FORMATS), max_height=0)
        assert best == (1080, 1920)

    def test_landscape_still_correct(self) -> None:
        """横屏视频不能因为这次改动而回归。"""
        landscape = [
            _fmt(137, 1920, 1080, "1080p"),
            _fmt(136, 1280, 720, "720p"),
            _fmt(135, 854, 480, "480p"),
        ]
        assert _sabr_best_quality(_FakeSabr(landscape)) == (1920, 1080)

    def test_ignores_audio_only(self) -> None:
        formats = [
            *SHORTS_FORMATS,
            {"itag": 140, "mimeType": 'audio/mp4; codecs="mp4a.40.2"', "qualityLabel": None},
        ]
        assert _sabr_best_quality(_FakeSabr(formats)) == (1080, 1920)

    def test_empty_formats(self) -> None:
        assert _sabr_best_quality(_FakeSabr([])) is None
