# 解析器里的格式适配（盘点，未改代码）

## 为什么盘这个

这些代码的**共同点是把结构编码进字符串**：`<a href>`、`**粗体**`、`> ` 引用前缀、
`{{media:N}}` 占位符，然后由渲染层用正则解析回来。

字符串既是数据又是语法。平台原文里只要出现与语法同形的字符，就会被误读 ——
于是只能再加 escape 补丁（`\#`、全角空格换缩进、`<i>` 包引用行），补丁之间
互相破坏。已知症状全在这一条链上：

- `\#C_Chat` 泄漏（序列化转义了，反序列化没还原）
- `</details>` 泄漏（序列化支持嵌套，解析器不支持）
- `</tg-collage>` 泄漏（inline 路径序列化后没有对应解析）
- 行首 4 空格变代码块 → 换全角空格 → 缩进宽度翻倍、对齐推歪

## 盘点结果：45 处，13 个文件

---

## A 生成 HTML 标签（17 处）

### `parsers/parser/bangumi.py`

- **L171** `escape/html:a` — `bits.append(f'<a href="{html.escape(entry.url, quote=True)}">{html.escape(entry.name)}</a>')`
### `parsers/parser/bilibili.py`

- **L114** `html:a` — `title = f'<a href="{url}">{raw_title}</a>'`
- **L172** `html:collage` — `return "<tg-collage>\n\n" + "\n".join(items) + "\n\n</tg-collage>"`
- **L205** `html:a` — `title_line = f'<a href="{page_url}">{title}</a>' if title and page_url else title`
- **L506** `escape/html:a` — `desc = desc.replace(f"#{name}#", f'<a href="{html.escape(url, quote=True)}">#{html.escape(name)}#</a>')`
- **L514** `html:a` — `return f'<a href="{url}">#{topic}#</a>'`
### `parsers/parser/ptt.py`

- **L97** `html:details/html:summary` — `parts.append(f"<details><summary>{summary}</summary>\n\n" + "\n".join(lines).rstrip() + "\n\n</details>")`
### `parsers/parser/threads.py`

- **L41** `escape/html:a` — `return f'<a href="{html.escape(url, quote=True)}">{html.escape(topic)}</a>'`
### `parsers/parser/twitter.py`

- **L236** `html:a/html:italic/md:quote` — `return f'> <i><a href="{href}">{label}</a></i>', []`
### `parsers/parser/weibo.py`

- **L220** `escape/html:a` — `f"#{name}#", f'<a href="{html.escape(url, quote=True)}">#{html.escape(name)}#</a>'`
### `parsers/parser/youtube.py`

- **L202** `escape/html:a` — `return f'<a href="{url}">{html.escape(title)}</a>'`
### `provider_api/bangumi.py`

- **L368** `escape/html:a` — `return f'<a href="{html.escape(href, quote=True)}">{text}</a>'`
- **L380** `html:bold` — `return f"<b>{text}</b>" if text else ""`
- **L384** `html:italic` — `return f"<i>{text}</i>" if text else ""`
- **L839** `escape/html:a` — `origin_line = f'**<a href="{url}">{html.escape(episode_name)}</a>**'`
- **L842** `escape/html:a` — `f'<a href="{html.escape(context_url, quote=True)}">{html.escape(context_name)}</a>'`
### `provider_api/youtube.py`

- **L375** `escape/html:a` — `text = f'<a href="{html.escape(real, quote=True)}">{html.escape(text)}</a>'`

## B 生成 markdown 语法（12 处）

### `parsers/parser/ptt.py`

- **L86** `md:quote` — `line = f"> {tag} {push.user}: {push.content}".rstrip()`
- **L95** `md:quote` — `lines.append(f"> …還有 {remaining} 條推文未顯示")`
### `parsers/parser/twitter.py`

- **L236** `html:a/html:italic/md:quote` — `return f'> <i><a href="{href}">{label}</a></i>', []`
### `provider_api/bangumi.py`

- **L565** `md:bold` — `"""楼中楼**回复的那一层**（DOM 上最近的 ˋˋpost_<id>ˋˋ 祖先）；一级楼层为空。`
- **L592** `md:bold` — `"""**本层**的作者（不是楼主 —— 楼主是主题的创建者，楼层可能不是他）"""`
- **L617** `md:bold` — `"""解析出来的楼层（仅供核对/测试，**不再渲染**）"""`
### `provider_api/twitter.py`

- **L637** `md:bold` — `"""正文里的标签 (不含 ˋˋ#ˋˋ), 来自平台实体 ˋˋentities.hashtags[].textˋˋ。`
- **L698** `md:heading` — `"header-one": lambda t: f"# {t}",`
- **L699** `md:heading` — `"header-two": lambda t: f"## {t}",`
- **L700** `md:heading` — `"header-three": lambda t: f"### {t}",`
- **L701** `md:quote` — `"blockquote": lambda t: "\n".join(f"> {line}" for line in t.split("\n")),`
### `types/result.py`

- **L159** `md:bold` — `"""ˋˋmediaˋˋ **开头**有几个是置顶图 (板头/banner); 渲染层提到标题上方并用分割线隔开"""`

## C 生成占位符（10 处）

### `parsers/parser/bilibili.py`

- **L193** `ph:media` — `base: Any, page_url: str, placeholder: str = "{{media:0}}"`
- **L262** `ph:media` — `marks = [f"{{{{media:{offset + i}}}}}" for i in range(len(reply.images or []))]`
- **L273** `ph:media` — `f"{{{{media:{target_offset + i}}}}}" for i in range(len(target.images or []))`
- **L285** `ph:media` — `base, page_url, placeholder=f"{{{{media:{video_start}}}}}"`
### `parsers/parser/threads.py`

- **L119** `ph:poll` — `return f"{{{{poll:{index}}}}}"`
### `parsers/parser/twitter.py`

- **L117** `ph:poll` — `return f"{{{{poll:{index}}}}}"`
### `parsers/parser/youtube.py`

- **L179** `ph:poll` — `return f"{{{{poll:{index}}}}}"`
### `provider_api/bangumi.py`

- **L848** `ph:trans` — `origin_line = f"**{label}** » {{{{t:讨论}}}}"`
### `provider_api/linuxdo.py`

- **L655** `ph:poll` — `placeholder.string = f"{{{{poll:{offset + len(structured) - 1}}}}}"`
### `types/result.py`

- **L170** `ph:poll` — `"""投票的结构化数据; 顺序 = 正文里 ˋˋ{{poll:N}}ˋˋ 占位符的出现顺序"""`

## D escape（16 处）

### `parsers/parser/bangumi.py`

- **L170** `escape` — `bits.append(f"**{html.escape(entry.rank)}.**")`
- **L171** `escape/html:a` — `bits.append(f'<a href="{html.escape(entry.url, quote=True)}">{html.escape(entry.name)}</a>')`
- **L173** `escape` — `bits.append(" / ".join(html.escape(job) for job in entry.jobs))`
- **L175** `escape` — `bits.append(html.escape(entry.info))`
### `parsers/parser/bilibili.py`

- **L506** `escape/html:a` — `desc = desc.replace(f"#{name}#", f'<a href="{html.escape(url, quote=True)}">#{html.escape(name)}#</a>')`
### `parsers/parser/threads.py`

- **L41** `escape/html:a` — `return f'<a href="{html.escape(url, quote=True)}">{html.escape(topic)}</a>'`
### `parsers/parser/twitter.py`

- **L234** `escape` — `href = html.escape(card.url, quote=True)`
- **L235** `escape` — `label = html.escape(card.title)`
### `parsers/parser/weibo.py`

- **L220** `escape/html:a` — `f"#{name}#", f'<a href="{html.escape(url, quote=True)}">#{html.escape(name)}#</a>'`
### `parsers/parser/youtube.py`

- **L202** `escape/html:a` — `return f'<a href="{url}">{html.escape(title)}</a>'`
### `provider_api/bangumi.py`

- **L368** `escape/html:a` — `return f'<a href="{html.escape(href, quote=True)}">{text}</a>'`
- **L839** `escape/html:a` — `origin_line = f'**<a href="{url}">{html.escape(episode_name)}</a>**'`
- **L842** `escape/html:a` — `f'<a href="{html.escape(context_url, quote=True)}">{html.escape(context_name)}</a>'`
- **L844** `escape` — `else html.escape(context_name)`
### `provider_api/facebook.py`

- **L107** `escape` — `match = re.search(_META_RE_TMPL.format(name=re.escape(name)), html)`
### `provider_api/youtube.py`

- **L375** `escape/html:a` — `text = f'<a href="{html.escape(real, quote=True)}">{html.escape(text)}</a>'`

---

## `lib/utils/helpers.py` —— 共享的标记产出点（**核心**）

这 5 个函数是所有平台共用的「把结构变成字符串」的出口。改渲染通道必须从这里下手，
否则每个平台各自拼标记，永远收不齐。

| 函数 | L | 产出 | 渲染层怎么解回去 |
| --- | --- | --- | --- |
| `media_placeholder(index)` | 26 | `{{media:N}}` | `replace_media_placeholders` → `![](tg://photo?id=mN)` |
| `format_author_label(name, handle)` | 99 | `名字 @handle`（带 `**`） | `parse_inline` 解析 |
| `format_author_link(name, handle, url)` | 170 | `<a href>**…**</a>` + `@handle` | `parse_inline` 解 `<a>`/`<b>` |
| `format_quote_block(text, author)` | 217 | `> <i>作者</i>` + 每行 `> ` | `split_quote_blocks` 按 `> ` 切 |
| `_quote_line(line)` | 261 | 给每行加 `> ` 前缀；表格行不包 `<i>` | 同上 |

`format_quote_block` 的 docstring 写着「⚠️ **新平台一律用这个函数**，不要自己拼
`> ` 前缀 —— 手写的版本会漏掉斜体等形态」。说明平台层已经在往共享出口收敛，
只是出口本身产出的是字符串。

---

## 改造顺序（按依赖）

1. `lib/utils/helpers.py` 的 5 个产出点 → 改成返回 Segment
2. `lib/parsers/parser/*.py` 里直接拼标记的地方 → 改成构造 Segment
3. `lib/provider_api/*.py` 的 markdown 转换（markdownify / API markdown）→ 直接产出 Segment
4. `plugins/parse/rich_blocks.py` 整个删除（不再需要解析）
5. `plugins/helpers.py` 里 8 个后处理函数删除
6. 发送路径统一到 blocks

第 1、2 步可以逐个平台做，每步真机验证；第 4、5 步必须等所有平台改完才能删。

---

## 渲染层对应的解析端（同样要删）


| 文件 | 作用 |
| --- | --- |
| `plugins/parse/rich_blocks.py::markdown_to_blocks` | markdown → blocks 的主解析器 |
| `plugins/parse/rich_blocks.py::parse_inline` | 行内语法解析（`**`、`<a>`、`<i>`） |
| `plugins/helpers.py::escape_telegram_markdown` | 给平台原文加反斜杠 |
| `plugins/helpers.py::escape_setext_underlines` | 防止 `---` 变标题 |
| `plugins/helpers.py::link_hashtags` | 把 `#tag` 变成链接 |
| `plugins/helpers.py::preserve_linebreaks` | 行尾补两个空格 |
| `plugins/helpers.py::format_text` | 正文的折叠与引用转换 |
| `plugins/helpers.py::split_quote_blocks` | 按 `> ` 前缀切引用块 |
| `plugins/helpers.py::replace_media_placeholders` | `{{media:N}}` → `![](tg://…)` |
| `plugins/helpers.py::wrap_collage` | `<tg-collage>` 包裹 |
| `plugins/helpers.py::fold_for_show_more` | `<details>` 折叠 |
| `plugins/helpers.py::_reindent` | 已删（换全角空格的补丁） |
