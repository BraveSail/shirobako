# 调用鉴权：本地库白名单（权限模型 v2）

## 用户定义的权限模型（2026-10-12 原话）

> 我先定义一下权限模型：在和bot同群的人发链接，这属于bot在群内的自动匹配触发机制，
> 不需要鉴权，而inline，匿名消息和私聊等非同群调用，则需要鉴权，现在要做出更改：
> 取消同群检查这个判定，只检查本地数据库，按照这个逻辑来实现
>
> 表名应该更改了，users下admin flag

## 模型

| 入口 | 鉴权 | 实现位置 |
|---|---|---|
| 群内自动匹配（群消息发链接） | **不鉴权**（群内触发机制） | `handlers.parse` 只对 PRIVATE 判定 |
| 私聊（含 `/jx` `/jxjx` `/raw` `/zip`） | **鉴权**（本次新增） | `handlers.parse` |
| inline | **鉴权**（含私聊唤起——旧豁免已废除） | `inline._call_inline_parse` |
| guest（匿名消息） | **鉴权** | `guest.guest_parse` |

**鉴权 = 只查本地数据库**：`users.role` 非空（`user`/`admin`）∪ 配置超管
（`.env` 的 `ADMIN_USERS`）。**不再有 `get_chat_member` 同群检查**。

## 数据模型变化

- `admin_users` 表**删除**，并入 `users` 表（用户指令「users 下 admin flag」）：
  - `role = NULL` —— 无授权（用过 bot 的普通路人；48 行存量用户全在此档）
  - `role = "user"` —— 白名单普通用户（`/add` 加）
  - `role = "admin"` —— 管理员（`/promote` 提升）
- 迁移 `d8e4f2a1b3c5`（revises `c4f1a2b3d5e6`）：加列 → 搬 `admin_users` 数据
  （users 里没有的行会补建）→ 校验 → drop 旧表。downgrade 可重建旧表。
- `remove()`（`/del`）语义变化：**清 role，不删 users 行**（行里还有语言设置）。
- 生产迁移实测结果：`258404034 → role=user`、`admin_users` 表已删、版本到 `d8e4f2a1b3c5`。

## 判定入口

- `is_whitelisted_user(uid)`（`services/admin_user.py` 便捷函数）—— 非同群调用鉴权。
  每次**直接查库**（不缓存）：本地 SQLite 查询远比解析轻，换来 `/add` `/del` 立即生效。
- `is_admin_user(uid)` —— 管理命令（配置超管 ∪ `role=admin`）。
- `is_super_admin(uid)` —— `/promote` `/demote`（只认配置）。
- 旧 `AccessGate` / `plugins/parse/access.py` **已删除**；`guest_whitelist_group_id`
  配置项**已删除**（.env 里的残留值被 pydantic `extra="ignore"` 忽略，可手动清理）。

## 关键测试（test/test_access_gate.py）

- 私聊路人被拒（`仅限白名单用户使用`）、白名单通过且进解析流程；
- **群消息不鉴权**：allowed=False 也进解析，且**连白名单查询都不发生**；
- inline 一律鉴权（含私聊唤起）；
- `is_whitelisted_user` 语义（超管短路不查库、role=None 路人拒、None 用户拒）。
- 另：`test_admin_commands.py`（两级 + promote/demote/del）、`test_guest_mode.py`（guest 入口）。

## 部署记录（2026-10-12）

- commit `8118150` 已推 main，161 部署成功；
- 真机验证：超管 True / `role=user` True（且 is_admin False）/ 路人 False / None False；
- 容器日志干净；迁移后 DB 状态：`users.role` 只 1 行非空（258404034=user）。

## 给用户的操作说明

- 谁不能用私聊/inline/guest：默认所有人（除了 .env 超管和已加白的人）。
- 加人：`/add <uid>`（普通用户）或 `/promote <uid>`（管理员）——都只影响非同群调用；
  群内发链接任何人都照常触发解析。
- 移除：`/del <uid>`（清 role；不改用户的语言等数据）。
