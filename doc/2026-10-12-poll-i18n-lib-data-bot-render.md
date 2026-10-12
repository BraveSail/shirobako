# 投票与归属行的本地化：lib 出数据、渲染层按语言拼

## 背景

用户问「没i18n吗？这种本地化都排查下」——起因是 threads 投票刚上线时，表格头
`| 选项 | 票数 | 占比 |` 在任何语言下都是中文。

**结构性根因**：i18n 引擎（easy_ai18n）只在 bot 层（`parse_hub_bot/plugins/`），
靠 AST **调用点**解析源文本查 16 语言 yaml；而投票表格、bgm 归属行这类**用户可见文案
在 lib（parsehub）层拼装**——lib 是纯解析库，**没有 locale 概念、没有 i18n 通道**。
两边各自都对，接缝上漏掉了。

同一类问题全量排查（本轮）：
| 位置 | 性质 | 处置 |
|---|---|---|
| poll 表格（twitter/threads/youtube/linux.do） | 正文内容里的固定文案 | **本轮改**（占位符方案） |
| bgm 归属行 `» 讨论` | 元信息区固定文案 | **本轮改**（`{{t:}}` 片段方案） |
| 平台显示名（微博/知乎/小红书…22 个） | 页脚「来源（微博）」 | 记录，待定（品牌专名，翻译有争议） |
| lib 抛的 ParseError 中文文案 | 错误消息（异常路径） | 单列，机制不同，未动 |
| `查看知乎原文`、`万/亿` | **解析匹配用**（输入侧） | 无需改 |

用户选定**方案 C：lib 只输出结构化数据，渲染层按 lang 本地化**。

## 方案与实现

### poll：`{{poll:N}}` 占位符

- **lib**：`Poll` / `PollOption` dataclass（`text` / `count` / `total_votes` / `voters` /
  `title` / `is_finished`）；结果对象带 `polls: list[Poll]`；正文里表格位置放
  **独立一行** `{{poll:N}}`（N = 占位符出现顺序，从 0 递增）。
  - 四个平台的索引顺序：twitter `reply → 主体 → quoted`；threads `reply → 主体`;
    youtube 仅主体；linux.do 按 `div.poll` DOM 序（上下文楼层在前）。
  - 引用块内占位符由 `_quote_line` 豁免（不包 `<i>`、不补行尾两空格），形态 `> {{poll:N}}`。
  - 序列化：`to_dict()` 带 `polls`；缓存往返 `_polls_to_payload` / `_polls_from_payload`
    （`count=None` 保真；**老缓存无 `polls` 键退化成 `[]`**）。
- **bot**：`replace_poll_placeholders(content, polls, lang)` 在 `build_rich_markdown` 的
  **一切结构处理之前**替换；`_render_poll` 按形态映射输出：
  - 有单项票数 → 三列 `| 选项 | 票数 | 占比 |`（占比按总票数）；
  - 全无单项票数（youtube）→ `投票 · 共 N 票` 头部 + 单列 `| 选项 |`；
  - linux.do → `📊 投票（「title」 · N 人参与）` 头部 + 三列。
  - 索引越界/数据空 → **整行删除 + warning**（占位符绝不泄漏）；`polls=[]` → 逐字不变。
  - 引用块内替换产物每行带 `> ` 前缀（空行写 `>`），表格行不包 `<i>`。

### bgm 归属行：`{{t:源文本}}` 可翻译片段

- **lib**：`origin_line = f"**{label}** » {{{{t:讨论}}}}"`。
  - ⚠️ **f-string 里要写四个花括号**：`{{{{t:讨论}}}}` —— f-string 把每对 `{{` 折成一个 `{`，
    写两个花括号输出只有一层（实测踩过：产出 `{t:讨论}` 匹配不上）。
- **bot**：`replace_translatable_fragments(text, lang)`，正则 `\{\{t:([^{}]+)\}\}`；
  对 **content 与 origin_line 两处**都调用。
  - ⚠️ **片段必须走字面量调用点**（`if fragment == "讨论": return t_[lang]("讨论")`）——
    **变量传参会让 easy_ai18n 在日志里留一条 `I18N parse error` ERROR**
    （库对"看不见调用点"的已知行为；生产累积日志里已有同类条目）。
    实测对照：`translate(fragment)` → ERROR；`t_[lang]("讨论")` → 零报错。

### i18n 词条（bot，10 条 × 16 语言）

| 源文本 | key |
|---|---|
| 选项 / 票数 / 占比 | ea15ae2b7fba / 8472b9d2a8a9 / c909e6eb65aa |
| 投票 | e7f40ca894a3 |
| 共 {total:,} 票 | d9d060bed1f0 |
| 📊 投票 | 7b000c997e5e |
| 「{title}」 | aef1e20b40db |
| {voters} 人参与 | fe76fe2b4eef |
| （{details}） | 38a42cd2eec9 |
| 讨论 | da86ae7e75fe |

手工插 16 yaml（12-hex 文本级插入；**没跑全量 i18n.build** —— 会清掉"字典值"活词条）。

## 渲染路径清单（改这类渲染参数时逐条对）

- `build_rich_markdown`（直发，主路径）✓
- `build_rich_markdown_by_str`（缓存）+ `_RichFields`（duck-type 带 `polls`）✓
- `rich_cache_entry`（写缓存）+ `CacheParseResult.polls` + 两处调用点 ✓
- `build_caption` / `build_caption_by_str`（老 caption 路径）✓
- `test_render_path_parity.py`（两条路逐字一致）✓

## 验证

- lib：934 passed / 1 skipped；ruff 干净。
- bot：606 passed；ruff 干净；i18n 逐语言校验 16/16。
- **形态保真**：改造前 lib 直拼的表格 vs 新占位符+polls 渲染，twitter / youtube / linux.do
  三种形态**逐字一致**（子代理实测 True/True/True）。
- 真机（161 部署后）：真实 threads 投票帖 → 日语 `選択肢/票数/割合`、英语 `Option/Votes/Share`、
  简中 `选项/票数/占比`；服务端块含 `RichBlockTable`；三条已发用户私聊验证。
- bgm 归属行三语言实测：`» 讨论` / `» Discussion` / `» 掲示板` / `» 討論`。
- 老缓存（无 `polls` 字段）不报错、行为不变（测试钉住）。

## 遗留

- **平台显示名**（22 个）走页脚「来源（微博）」——品牌专名翻译有争议，需要时问用户。
- **错误消息**（lib 的 ParseError 中文文案）——异常路径需要错误码契约，另开任务。
- jp/等语言下 `view_label` 的「表示」等词条是既有机制，不属本轮。
