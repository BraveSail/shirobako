"""bilibili 图文动态（opus）: ``t.bilibili.com/<id>`` 也必须走详情页拿全文。

接口 ``polymer/web-dynamic/v1/detail`` 对图文只给截断摘要（``summary.has_more=true``），
全文和图片顺序在详情页的 ``__INITIAL_STATE__.detail.modules`` 里。

判据以前是「URL 里有没有 ``/opus/``」—— ``t.bilibili.com/<id>`` 这种动态链接
同样被截断却拿不到全文（实测 1265762239826100230: 接口 222 字/1 图，
详情页 510 字/4 图）。现在改成无条件先试详情页、取不到再回退接口。
"""

from parsehub.provider_api.bilibili import BiliDynamic

DYN_ID = "1265762239826100230"


def _page_detail(*, paragraphs: list[dict] | None = None, banner: str = "") -> dict:
    """详情页 ``__INITIAL_STATE__.detail`` 的最小形态。"""
    modules: list[dict] = []
    if banner:
        modules.append(
            {
                "module_type": "MODULE_TYPE_TOP",
                "module_top": {"display": {"album": {"pics": [{"url": banner}]}}},
            }
        )
    modules.extend(
        [
            {"module_type": "MODULE_TYPE_TITLE", "module_title": {"text": "周榜"}},
            {
                "module_type": "MODULE_TYPE_AUTHOR",
                "module_author": {"name": "名作之壁吧", "mid": 123, "pub_ts": 1793347881},
            },
            {
                "module_type": "MODULE_TYPE_CONTENT",
                "module_content": {"paragraphs": paragraphs if paragraphs is not None else []},
            },
            {"module_type": "MODULE_TYPE_STAT", "module_stat": {"like": {"count": 7}}},
        ]
    )
    return {"id_str": DYN_ID, "modules": modules}


def _para(text: str = "", pics: list[dict] | None = None) -> dict:
    para: dict = {"para_type": 1}
    if text:
        para["text"] = {"nodes": [{"word": {"words": text}}]}
    if pics:
        para["pic"] = {"pics": pics}
    return para


def test_opus_page_item_keeps_all_paragraphs():
    """详情页的每一段（含纯图片段）都要保留 —— 摘要版只有前几段。"""
    paras = [_para(f"第{i}段") for i in range(16)]
    item = BiliDynamic.item_from_opus_page(_page_detail(paragraphs=paras))
    assert item is not None
    got = item["modules"]["module_dynamic"]["major"]["opus"]["summary"]["paragraphs"]
    assert len(got) == 16


def test_opus_page_item_keeps_banner_out_of_body_paragraphs():
    """``opus.pics`` 只放**板头图**（module_top），正文段落的图留在 paragraphs 里。

    两者混放会让正文图被当成 banner、并在正文里重复出现（实测：4 张正文图被
    数成 banner，media 从 4 张涨到 8 张）。
    """
    detail = _page_detail(
        paragraphs=[
            _para("文字"),
            _para(pics=[{"url": "https://i0.hdslb.com/body.jpg", "width": 1, "height": 1}]),
        ],
        banner="https://i0.hdslb.com/banner.jpg",
    )

    item = BiliDynamic.item_from_opus_page(detail)
    assert item is not None
    pics = item["modules"]["module_dynamic"]["major"]["opus"]["pics"]
    assert [p["url"] for p in pics] == ["https://i0.hdslb.com/banner.jpg"]
    # 正文图仍在段落里，没被搬进 pics
    paras = item["modules"]["module_dynamic"]["major"]["opus"]["summary"]["paragraphs"]
    body_pics = [
        pic["url"]
        for para in paras
        for pic in ((para.get("pic") or {}).get("pics") or [])
    ]
    assert body_pics == ["https://i0.hdslb.com/body.jpg"]


def test_opus_page_item_returns_none_without_paragraphs():
    """没有正文段（纯视频动态）返回 None，让调用方回退接口 —— 不能变成硬失败。"""
    assert BiliDynamic.item_from_opus_page(_page_detail(paragraphs=[])) is None


def test_opus_page_item_marks_summary_not_truncated():
    """详情页给的就是全文，``has_more`` 必须是 False（否则渲染层还会当它被截断）。"""
    item = BiliDynamic.item_from_opus_page(_page_detail(paragraphs=[_para("正文")]))
    assert item is not None
    summary = item["modules"]["module_dynamic"]["major"]["opus"]["summary"]
    assert summary["has_more"] is False
