# Twitter 长文粗体（richtext）支持

## 问题（2026-10-13 用户报障）

推文 https://x.com/MaxForAI/status/2108035230003274189 原文有粗体段落，解析后没有任何样式。

## 数据来源（已核实）

长推文的局部加粗在：

```
node["note_tweet"]["note_tweet_results"]["result"]["richtext"]["richtext_tags"]
= [{"from_index": 87, "to_index": 156, "richtext_types": ["Bold"]}, ...]
```

- 该推文有 **9 个 Bold 区间**，索引指向 `note_tweet...text` 的**原始文本**（未展开短链）。
- `richtext` 只在 `note_tweet` 里（普通短推文没有这个能力，公开 schema 亦如此）。
- 之前代码完全没读这个字段（grep 零命中），所以粗体全丢。

## 实现

### lib（provider）

`lib/src/parsehub/provider_api/twitter.py`：

- 模块级 `_INLINE_STYLES`（`Bold`→`**` / `Italic`→`*` / `Strikethrough`→`~~`）。
- `_apply_richtext(text, richtext)`：区间从后往前插入标记（后面的插入不动前面的索引）；
  越界/空区间**跳过**（宁可少裹一段，不能让整条解析炸掉）；未知类型忽略。
- 调用点：`_parse_result` 里 **在 `_restore_short_urls` 之前**（索引指向未展开的文本，
  先展开会让区间漂移）。note_tweet 与 legacy 两条路径都接（legacy 的空值安全）。
- 被引用推文递归走同一条 `_parse_result`，自动获得同样处理。

### 缓存作废（关键，缺了等于没修）

用户重发同一条链接会命中缓存 —— 旧条目里存的是**没有 `**` 的正文**，
不复位就永远看不到变化。两层都加了 `content_version` 判据（当前值 1）：

- 结果层（lib `serialize.py`）：`result_from_cache_dict` 对 **twitter** 且
  `content_version < 1` 抛 `ResultRebuildUnavailable` → `ResultCache.get` 删 key 按未命中处理。
- 持久层（bot `services/cache.py`）：`CacheParseResult.content_version`，
  `PersistentCache.get` 对 **twitter** 且 `< 1` 返回 None（重新解析）。
- **只对 twitter 强制** —— 其它平台正文格式没变，强制重解析只会白下媒体。
- 写入端（`rich_cache_entry`）写 1。

## 验证

- 单测：lib 新增 11（区间、多区间、越界、短链顺序、引用推文、真实 fixture 9 段）+
  4（结果层版本）；bot 新增 4（持久层版本、写入端带版本）。全量 lib 949 + bot 630 绿。
- 真机（161）：
  - 清该链接两层缓存 → 解析 → **9 个粗体段全部还原**；
  - 走完整 `build_rich_markdown` 渲染 → 粗体存活（9 正文 + 1 作者行）。
- `**` 在 Telegram 富文本里就是粗体（既有作者行每天在用，无需额外验证）。

## 文件

- `lib/src/parsehub/provider_api/twitter.py`（`_apply_richtext` + `_INLINE_STYLES` + 接线）
- `lib/src/parsehub/types/serialize.py`（结果层版本判据 + 常量）
- `services/cache.py`（持久层版本判据 + 字段）
- `plugins/parse/inline_rich.py`（写入端写版本）
- `lib/test/test_twitter_richtext.py` + `lib/test/fixtures/twitter_richtext_note.json`
- `lib/test/test_cache_content_version.py`、`test/test_cache_content_version.py`

## commit

`8679493`（feat(twitter): render the richtext bold of long notes, expire the old cache）
