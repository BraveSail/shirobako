# Threads 投票（poll）抓不到：数据在 caption_add_on 里，没人读

## 症状

用户分享 `https://www.threads.com/@snowmaple_official/post/DeY4kZBGnhG`（带投票的帖子），
bot 解析结果里**没有投票** —— 只有正文，选项和票数整段丢失。

## 根因（已取证）

投票数据在 `post["caption_add_on"]["poll"]`（`__typename` = `XDTPollSticker`）：

```json
{"poll": {"__typename": "XDTPollSticker", "expires_at": 1791882759,
  "finished": false,
  "tallies": [{"count": 11, "text": "あいはんする"}, {"count": 8, "text": "そうはんする"}],
  "viewer_can_vote": false, "viewer_vote": null, "poll_id": "17906331861593035"}}
```

而 `ThreadsPost.from_graphql` 只读 caption / snippet / spoilers / media —— **没有任何代码
碰过 `caption_add_on`**，所以投票被静默丢弃（与 2026-10-10 的 snippet 丢失同一形状：
"caption 底下的附加内容"没有统一读法，每个键都得单独接线）。

**票数就在同一次响应里**（`tallies[].count`）—— 不需要额外请求，与 linux.do
（`post["polls"]`，2026-10-06）和 twitter（`node["card"]`，2026-10-09）的结论一致。
Threads 的 GraphQL 是**单次请求**这条项目核心约束下的又一处"响应里本来就有"。

## 改动

### `parse_hub_bot`（commit `797badf`，单仓：lib/ 是 workspace 成员）

- `lib/src/parsehub/provider_api/threads.py`：
  - 新增 `ThreadsPoll` dataclass（`choices: list[tuple[str, int]]` + `is_finished`）；
  - `ThreadsPost.poll` 字段 + `_parse_poll()`：读 `caption_add_on.poll.tallies`，
    选项文案缺 / 全空时返回 `None`（不产出空投票），票数缺失按 0 算；
  - `from_graphql` 接线。
- `lib/src/parsehub/parsers/parser/threads.py`：
  - `_build_poll()`：渲染成与 twitter / linux.do **同一形态**的三列表格
    （`| 选项 | 票数 | 占比 |`，占比按总票数算、`|` 转义）；
  - `_do_parse` 的拼接顺序：被回复帖引用块 → caption 正文 → **投票表格** → 长文引用块；
  - `_build_quote`：被回复帖带投票时表格进它自己的引用块（与 twitter 一致）。

### 测试

- `lib/test/test_threads_poll.py`（14 条）+ fixture `threads_poll.json`（真实响应）。
- 覆盖：解析（真实数据/缺票数/空 tallies/无 poll）、渲染（三列/占比/正文在上/0 票/
  `|` 转义/无投票无表格/空选项）、长文与投票共存、引用块内表格（`> ` 前缀 + 无 `<i>` 包裹）。

## 验证

- 本机：lib 全量 **923 passed, 1 skipped**（含新 14 条）；`ruff check .` 干净；
  root 侧 `pytest test/` 576 passed、`uvx ruff check plugins/ test/` 干净。
- 部署：`deploy_remote.sh`（pull → build → up -d --force-recreate → 就绪观测 4s）。
- 容器内 `ParseService().parse(真实链接)` 实测：
  `| あいはんする | 12 | 60% | / | そうはんする | 8 | 40% |`（票数实时）。
- 真机发送（发到用户私聊，message_id=1284）后读回服务端块：
  `RichBlockParagraph → RichBlockDivider → RichBlockParagraph → RichBlockTable →
  RichBlockDivider → RichBlockFooter` —— **`RichBlockTable` 出现**，确认渲染层真的
  把它当成 Table 块（不是字面竖线文本）。

## 遗留与注意事项

- **`caption_add_on` 里可能还有别的贴纸**（当前只处理 `poll`）；发现新贴纸丢失时先
  dump 这个键，别假设"没有"。
- 投票的 `expires_at` / `viewer_vote` 字段没有渲染（与 twitter 的 `end_datetime` 同处理，
  只显示选项/票数/占比三列；需要"截止时间"或"我的投票"时再加）。
- 历史消息缓存里没有 poll 字段 —— 存量结果不会追溯出现投票（重新解析即可）。
