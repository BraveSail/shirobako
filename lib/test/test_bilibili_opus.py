"""bilibili opus：详情页转全量 item + 段落渲染（图片原位标记）。

背景（2026-10-22 用户报「抓不全，图片不在原位」）：
- ``detail`` 接口对 opus 只回**截断摘要**（``summary.has_more = true``，实测 4 段 103 字、1 张图）；
- 全文与图片顺序只在 opus 详情页的 ``__INITIAL_STATE__.detail.modules`` 里
  （同一内容 165 段 / 2854 字 / 35 张图）。
"""

from parsehub.provider_api.bilibili import BiliDynamic


def _page_detail() -> dict:
    """opus 详情页 ``detail`` 的最小形态（modules 是**列表**，与接口的字典不同）。"""
    return {
        "id_str": "1261967911063388214",
        "modules": [
            {"module_type": "MODULE_TYPE_TITLE", "module_title": {"text": "标题"}},
            {
                "module_type": "MODULE_TYPE_AUTHOR",
                "module_author": {"mid": 3493265644980448, "name": "蔚蓝档案", "pub_ts": "1792663201"},
            },
            {
                "module_type": "MODULE_TYPE_CONTENT",
                "module_content": {
                    "paragraphs": [
                        {"para_type": 2, "pic": {"pics": [{"url": "https://x/1.png", "width": 1280, "height": 720}]}},
                        {
                            "para_type": 1,
                            "text": {"nodes": [{"type": "TEXT_NODE_TYPE_WORD", "word": {"words": "第一段"}}]},
                        },
                        {
                            "para_type": 8,
                            "heading": {
                                "level": 1,
                                "nodes": [{"type": "TEXT_NODE_TYPE_WORD", "word": {"words": "小标题"}}],
                            },
                        },
                        {"para_type": 2, "pic": {"pics": [{"url": "https://x/2.png", "width": 1080, "height": 7039}]}},
                        {
                            "para_type": 1,
                            "text": {"nodes": [{"type": "TEXT_NODE_TYPE_WORD", "word": {"words": "第二段"}}]},
                        },
                    ]
                },
            },
            {"module_type": "MODULE_TYPE_STAT", "module_stat": {"like": {"count": 1061}}},
        ],
    }


def test_page_modules_are_folded_into_the_api_item_shape():
    """页面 modules 列表 → 接口的 modules 字典；段落原样保留、has_more 置 False。"""
    item = BiliDynamic.item_from_opus_page(_page_detail())
    assert item is not None
    modules = item["modules"]
    assert modules["module_author"]["mid"] == 3493265644980448
    assert modules["module_stat"]["like"]["count"] == 1061
    major = modules["module_dynamic"]["major"]
    assert major["type"] == "MAJOR_TYPE_OPUS"
    summary = major["opus"]["summary"]
    assert summary["has_more"] is False
    assert len(summary["paragraphs"]) == 5
    # 图片按段序收集（两处 pic 段落）
    assert [p["url"] for p in major["opus"]["pics"]] == ["https://x/1.png", "https://x/2.png"]


def test_page_without_paragraphs_returns_none():
    """没有正文段（结构变了/空内容）时返回 None，调用方回退接口。"""
    assert BiliDynamic.item_from_opus_page({"modules": []}) is None
    assert BiliDynamic.item_from_opus_page({}) is None


def test_paragraphs_render_with_in_place_media_markers():
    """文本/标题/图片按段序渲染：图留 {{media:N}} 标记，下标与 images 顺序一致。"""
    item = BiliDynamic.item_from_opus_page(_page_detail())
    dynamic = BiliDynamic.parse({"item": item})

    assert dynamic.title == "标题"
    assert dynamic.content == "{{media:0}}\n\n第一段\n\n**小标题**\n\n{{media:1}}\n\n第二段"
    assert [i.url for i in dynamic.images] == ["https://x/1.png", "https://x/2.png"]
    # 标记下标必须覆盖 0..n-1 且与媒体数一致
    import re

    markers = [int(x) for x in re.findall(r"\{\{media:(\d+)\}\}", dynamic.content)]
    assert markers == list(range(len(dynamic.images)))


def test_truncated_api_summary_still_falls_back_to_plain_text():
    """has_more=true（接口摘要）时不能按段落渲染 —— 那只有前几段，退回 summary.text。"""
    dynamic = BiliDynamic.parse(
        {
            "item": {
                "modules": {
                    "module_dynamic": {
                        "major": {
                            "type": "MAJOR_TYPE_OPUS",
                            "opus": {
                                "title": "t",
                                "summary": {
                                    "text": "截断的正文",
                                    "has_more": True,
                                    "paragraphs": [
                                        {
                                            "para_type": 1,
                                            "text": {
                                                "nodes": [
                                                    {"type": "TEXT_NODE_TYPE_WORD", "word": {"words": "[图片]"}}
                                                ]
                                            },
                                        }
                                    ],
                                },
                                "pics": [{"url": "https://x/cover.png", "width": 1, "height": 1}],
                            },
                        }
                    }
                }
            }
        }
    )
    assert dynamic.content == "截断的正文"
    assert "{{media:" not in dynamic.content
    assert [i.url for i in dynamic.images] == ["https://x/cover.png"]
