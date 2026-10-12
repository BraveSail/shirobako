# 两级权限：`/add` 加普通用户，`/promote` 加管理员（仅超管）

## 背景（2026-10-12 用户需求）

原模型是**单层白名单**：配置 `ADMIN_USERS` ∪ DB `admin_users` 表，唯一的判定
`is_admin_user()` 同时管着"谁在名单里"和"谁能用管理命令"。用户要求拆成两级：

- `/add` → 白名单**普通用户**（现有白名单保持这个身份，不自动升级）
- `/promote` → **管理员**；**`/promote` 只能由用户本人（超管）使用**
- 普通用户可以使用 bot（解析主功能对所有人开放——现状即如此）
- 「与 bot 同群自动可用」规则**保持不变**（澄清：那两条机制详见下）

## 权限模型（现状的准确描述，改动前已核实）

「用 bot 的资格」在现实代码里**不是**由 `admin_users` 表管的，而是两条独立机制：

1. **私聊/群消息解析**：无身份 gate —— 任何能发消息的人都能用（`plugins/parse/handlers.py`）。
2. **inline / guest**：`plugins/parse/access.py::AccessGate` 按**运行时**「与 bot 同在
   `GUEST_WHITELIST_GROUP_ID` 群」判定（TTL 缓存，不写库）。

`admin_users` 表/`ADMIN_USERS` 配置**只管管理命令**。所以本次改动 = 给管理命令拆两级 +
`/add` 语义降级为"普通"，**不碰**上面两条。

## 改动

### 数据模型

`db/models/admin_user.py` 加列：

```python
role: Mapped[str] = mapped_column(String(16), nullable=False, server_default=ROLE_USER)
# ROLE_USER = "user"（普通白名单）; ROLE_ADMIN = "admin"（管理员）
```

迁移 `alembic/versions/c4f1a2b3d5e6_add_role_to_admin_users.py`：
`op.add_column` + `server_default="user"`（**既有行回填 user** = 现有白名单保持普通）；
`create_all` 建的新库不再执行加列（迁移里先查列存在）。

### 服务层（`services/admin_user.py`）

| 方法 | 语义 |
|---|---|
| `is_admin(uid)` | **管理员判定**（管理命令用）：配置超管 ∪ DB `role=admin`。**普通用户不是** |
| `is_whitelisted(uid)` | 名单语义：配置 ∪ DB（任意角色） |
| `add(uid)` | `/add`：写 `role=user` |
| `promote(uid)` | `/promote`：role=user → admin；不在表里则**直接插入 admin**；配置超管 → False |
| `demote(uid)` | `/demote`：admin → user；配置超管**降不了**（调方先查 `is_configured`） |
| `is_configured(uid)` | 是否配置超管（静态；原 `in_configured` 改名） |
| `list_roles()` | `[(uid, "super"\|"admin"\|"user")]`（原 `list_ids` 的带角色版） |

便捷判定：`is_admin_user(uid)`（管理员或超管）、`is_super_admin(uid)`（仅配置）。

### 命令层（`plugins/admin.py`）

- 守卫拆两档：`_require_admin`（管理命令）/ `_require_super_admin`（`/promote` `/demote`）。
- `/add`：写普通用户；提示语「已加入白名单(普通用户)」。
- `/promote <uid>` / `/demote <uid>`：仅超管；支持 uid / @username / 回复消息三种给法
  （复用 `_resolve_target`）。
- `/list`：**分三段**（超管 / 管理员 / 普通用户）。
- `/del`：按目标删除该行（不论角色）；配置层提示改 .env（不变）。
- 管理命令**不进公开菜单**（既有约定）。

### i18n

新词条 9 条 × 16 语言（`已加入白名单(普通用户)` / `已提升为管理员` / `已是管理员` /
`该命令仅限超管使用` / `已降为普通用户` / `不是管理员` / `超管` / `管理员` / `普通用户`），
12-hex 文本级插入。`无权使用该命令` 等旧词条复用。

## 验证

- 单测：**619 passed**（原 604 + 新增 15：两级判定边界、promote/demote 幂等与新建、
  非超管调 `/promote` 被拒且无副作用、配置超管不可降、`/list` 三段）。ruff 干净。
- **迁移生产场景实测**（本机临时库）：旧表（无 role）+ 既有行 → `upgrade head` →
  role 列加上、既有行回填 `user`。
- **真机（161 部署后）实测**：
  - `is_super_admin(超管)` True；`is_admin_user(普通白名单)` **False**；`is_whitelisted` True；
  - 陌生人 False；
  - `promote` → `is_admin_user` True → `demote` → False（往返正确）；
  - `list_roles` = `[(258404034, 'user'), (1879026273, 'super')]`；
  - 容器日志无异常，handler 全部注册（`promote_user` / `demote_user` 等 7 个）。

## 给用户的使用说明

- `/add <uid>` 或回复某人 → 白名单普通用户（可用 bot 的全部普通功能）。
- `/promote <uid>` 或回复某人 → **管理员**（能用 `/purge` `/list` `/del`）。只有你能调。
- `/demote <uid>` → 降回普通。只有你能调。
- 你（超管）在 `.env` 的 `ADMIN_USERS` 里 —— 这个列表**不能被聊天命令修改**
  （`/del`/`/demote` 对配置层会提示改 .env）。

## 遗留

- `ADMIN_USERS` 语义收窄为"超管"（实际只放用户本人，语义清晰化，值未变）。
- 旧白名单那 1 行（258404034）保持 `user`；要升管理员就 `/promote 258404034`。
