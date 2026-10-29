"""YouTube 直播识别（``_is_live``）+ 直播的 SABR 输入。

直播的流没有 contentLength / approxDurationMs，点播那套"按时长跑完就停"
对它无效 —— 卡在下载中就是这么来的。所以解析阶段必须认出来，并告诉
取流工具按墙钟计时。
"""

from parsehub.provider_api.youtube_sabr import _build_input_payload
from parsehub.provider_api.youtube_video import _is_live


def _player(**details) -> dict:  # noqa: ANN003 - 拼测试数据
    return {"videoDetails": details}


def test_videodetails_is_live():
    assert _is_live(_player(videoId="x", isLive=True)) is True


def test_live_content_with_zero_length():
    """isLiveContent 且 lengthSeconds=0 —— 正在直播"""
    assert _is_live(_player(videoId="x", isLiveContent=True, lengthSeconds="0")) is True


def test_live_now_in_microformat():
    data = {
        "videoDetails": {"videoId": "x", "lengthSeconds": "0"},
        "microformat": {"playerMicroformatRenderer": {"liveBroadcastDetails": {"isLiveNow": True}}},
    }
    assert _is_live(data) is True


def test_finished_broadcast_is_not_live():
    """下播后的录播: 有 liveBroadcastDetails 但 lengthSeconds > 0"""
    data = {
        "videoDetails": {"videoId": "x", "lengthSeconds": "7200"},
        "microformat": {"playerMicroformatRenderer": {"liveBroadcastDetails": {"isLiveNow": False}}},
    }
    assert _is_live(data) is False


def test_normal_video_is_not_live():
    assert _is_live(_player(videoId="x", lengthSeconds="213")) is False
    assert _is_live({}) is False


def test_sabr_payload_carries_live_flag():
    """直播要把 isLive/liveSeconds 传给工具 —— 它靠墙钟计时停下"""
    payload = _build_input_payload(
        "vid",
        abr_url="https://example/abr",
        ustreamer_config="cfg",
        adaptive_formats=[{"itag": 299}],
        cookie="a=b",
        video_quality="1080p",
        is_live=True,
        live_seconds=30,
    )
    assert payload["isLive"] is True
    assert payload["liveSeconds"] == 30


def test_sabr_payload_normal_video_has_no_live_flag():
    """点播的 payload 里不该出现直播字段（工具据此走"按时长跑完"的老路）"""
    payload = _build_input_payload(
        "vid",
        abr_url="https://example/abr",
        ustreamer_config="cfg",
        adaptive_formats=[{"itag": 299}],
        cookie="a=b",
        video_quality="1080p",
    )
    assert "isLive" not in payload
    assert "liveSeconds" not in payload
