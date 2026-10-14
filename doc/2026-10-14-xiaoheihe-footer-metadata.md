# 小黑盒页脚：数据本来就在同一次响应里

日期：2026-10-14
起因：用户问「拿不到页脚数据？」—— 上一条链接的消息只有 `来源（小黑盒）` 一行。

## 结论：不是拿不到，是我们的代码没取

`link/tree` 响应的 `link` 段里**本来就有**页脚要的全部字段（同一次请求，不需要第二次）：

| 页脚那一段 | 字段 | 实测值 |
| --- | --- | --- |
| 发布时间 | `link.create_at`（unix 秒） | `1791939655` → 2026-10-14 09:00 (+08) |
| 点赞 | `link.link_award_num`（界面叫「赞」） | 69（复验时 72） |
| 回复 | `link.comment_num` | 76（复验时 79） |
| 作者标识 | `link.user.userid` | 49946265 |
| 浏览量 | **没有** | 42 个字段全翻过，无 view/click 类计数 |

取证方式：探针把整份响应落盘成 fixture（`lib/test/fixtures/xiaoheihe_link_tree.json`），
再离线把所有字段打出来 —— 不是只读渲染产物就下"没有"的结论。

> 之前 `references/platform-metadata-fields.md` 里记的「xiaoheihe：`create_at` / `link_award_num` /
> `comment_num`」是对的（已实测确认），只是**代码一直没接**，所以页脚空着。

## 改动

**① 库侧 provider**（`provider_api/xiaoheihe.py`）

`XiaoHeiHePost` 加 `author_id` / `published_at` / `like_count` / `reply_count`，
在 `parse()` 里从**同一次响应**取（`to_datetime` / `to_int` 归一化），video 与图文两条分支都带。

**② 库侧 parser**（`parsers/parser/xiaoheihe.py`）

四个结果类型（Video / Image / Multimedia / RichText）都转发页脚字段。
作者标识走 **`author_handle` = 数字 uid**（小黑盒没有 @用户名；同抖音/bgm 的约定：
没有用户名时用 uid 顶上，不让 @ 那段整个消失），并用 `profile_url` 拼主页地址
→ `PROFILE_URL_TEMPLATES` 补 `Platform.XIAOHEIHE = https://www.xiaoheihe.cn/app/bbs/user/{id}`。

**`view_count` 保持 None**（「有就填、没有就 None」硬规则，不为凑格式填 0）。

**③ 缓存**（`services/cache.py` + `plugins/parse/inline_rich.py`）

新增 `CacheParseResult.meta_fields` + `FOOTER_META_FIELDS_VERSION`，写入端带当前版本，
读取端低于当前版本即重新解析。

**为什么用版本号而不是字段名判**：`reply_count` 这类字段名早就在 schema 里，
老条目写的是 `None`，而**平台真没有**（小黑盒确实无浏览量）也是 `None` ——
两者在缓存里长得一模一样，`model_fields_set` 也区分不了。不升版本的话，
用户重发同一条链接命中的还是老条目，页脚那几段永远看不到（症状与"没修"一样）。

⇒ **给新平台接页脚字段时把 `FOOTER_META_FIELDS_VERSION` +1**。

## 验证

**离线**

| 用例 | 钉住的行为 |
| --- | --- |
| `test_xiaoheihe_footer_metadata_from_same_response` | 四个字段从同一次响应映射对（用真实 fixture 原值） |
| `test_xiaoheihe_parser_forwards_footer_metadata` | 解析层真的转发到 result（含 handle/url 拼装） |
| `test_xiaoheihe_missing_fields_stay_none` | 缺字段留 None、不抛 |
| `test_cache_footer_meta.py`（4 例） | 版本不足重解析 / 当前版本命中 / **写入端必须带版本**（否则无限重解析） |

现有 6 个"当前格式条目"的测试 fixture 补上了新字段（正是落地清单要求的"所有构造点都要填"）。

全量：lib `959 passed / 1 skipped`，bot `642 passed`，`ruff` + `pylint --errors-only` 干净。

**真机**（部署后，161 容器，同一条链接）

```
published_at  = 2026-10-14 01:00:55+00:00
view_count    = None            ← 平台确实没有
like_count    = 72
reply_count   = 79
author_handle = '49946265'
author_url    = https://www.xiaoheihe.cn/app/bbs/user/49946265
```

渲染产物页脚：

```
<footer><tg-time unix="1791939655" format="Dt">2026年10月14日 09:00</tg-time> · 72 点赞 · 79 回复
 · <a href="…/app/bbs/link/970772a04ea8">来源（小黑盒）</a></footer>
```

作者行：`**<a href="…/user/49946265">爱猪人士o</a>** <code>@49946265</code>`

**两条渲染路径逐字一致**（现场 `build_rich_markdown` vs 缓存 `build_cached_rich_content`）：页脚字符串相等。

**未完成的一步（如实记录）**：真机**发送**没能跑完 —— 探针反复起临时 pyrogram client
撞上 `[420 FLOOD_WAIT_X] Please wait ~1700s`。页脚内容的正确性已由上面两条渲染路径 + 库侧用例钉住；
发送链路本次没改动（上一条 PHOTO_EXT 修复已实测发得出去）。

## 顺带清掉的现场

探针为跑缓存路径往生产 Redis 写了一条 media 为空的测试缓存 —— 已用 `PersistentCache.remove()`
删掉并复验不存在（留着会让用户下次发这条链接命中"无媒体"的条目）。

## 仍缺的字段

**浏览量**：平台响应里没有（不是我们没取）。要显示得换别的接口/客户端形态，
属"是否值得多一次请求"的架构决策，**没有用户点头不加**。
