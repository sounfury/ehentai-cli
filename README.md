# ehentai-cli

[![CI](https://github.com/sounfury/ehentai-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/sounfury/ehentai-cli/actions/workflows/ci.yml)

通过 Cookie 登录，管理 **E-Hentai / EXHentai 在线收藏**的独立命令行工具。适合日常整理、批量脚本和 AI agent，无需启动 JHenTai。

支持 Python 3.10+，Windows / macOS / Linux。

- Cookie 登录、身份验证、退出；可显式导入本机 JHenTai 登录态。
- 查询十个收藏分组、搜索、游标分页、完整导出、读取收藏备注。
- 单项和批量添加、移动、修改备注、移除；移动时默认保留原备注。
- 网站原生批量移动、两个收藏夹合并、收藏夹重命名。
- 操作预览、保存计划、重复执行、逐项验证、冲突检测、结果记录、回滚。
- 所有业务输出均为 JSON，方便 AI 和脚本解析。

## 安装

目前从 GitHub 安装，尚未发布到 PyPI。任选一种方式：

```bash
uv tool install git+https://github.com/sounfury/ehentai-cli.git
# 或
pipx install git+https://github.com/sounfury/ehentai-cli.git
```

也可以在虚拟环境中安装：

```bash
python -m venv .venv
# Windows
.venv\Scripts\python -m pip install git+https://github.com/sounfury/ehentai-cli.git
.venv\Scripts\ehentai --help
# macOS / Linux
.venv/bin/python -m pip install git+https://github.com/sounfury/ehentai-cli.git
.venv/bin/ehentai --help
```

以下示例中的 `ehentai` 指上述安装得到的命令；Windows 也可以用 `py -m ehentai_cli`，前提是包已安装到该 Python 环境。

## 登录

第一版使用 Cookie 登录。在浏览器登录网站后，复制请求中的 Cookie header，通过隐藏输入提交：

```bash
ehentai login
```

Cookie 必须包含 `ipb_member_id` 和 `ipb_pass_hash`；EX 访问可能还需要有效的 `igneous`。登录会实际请求收藏页验证权限，验证成功才保存凭据。

```bash
# 从文件读取：Cookie header、JSON 字典、浏览器 Cookie 数组、Netscape cookies.txt
ehentai login --cookie-file /private/path/cookies.txt

# 从标准输入读取一行 Cookie，适合自动化
ehentai login --cookie-stdin

# 使用 EX，分别验证该站点的访问权限
ehentai login --site ex --cookie-file /private/path/cookies.txt

# 显式读取本机 JHenTai 登录态；数据库始终以只读方式打开
ehentai login --from-jhentai
# 自定义安装或未自动找到数据库时
ehentai login --from-jhentai --jhentai-db /path/to/db.sqlite

ehentai whoami
ehentai whoami --site ex
ehentai logout
```

不接受把原始 Cookie 放到命令参数中的 `--cookie` 选项，避免凭据进入 shell 历史和进程列表。Cookie 输入、请求头和响应正文不会出现在工具日志或错误信息中。

默认配置位置：

| 系统 | 配置目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\ehentai-cli` |
| macOS | `~/Library/Application Support/ehentai-cli` |
| Linux | `$XDG_CONFIG_HOME/ehentai-cli` 或 `~/.config/ehentai-cli` |

可通过 `EHENTAI_CONFIG_DIR` 指定独立目录，例如为不同账号分别配置。Windows 凭据使用当前用户的 DPAPI 加密；macOS/Linux 使用权限为 `0600` 的凭据文件。`logout` 删除保存的凭据，保留操作历史以便核对和恢复。

JHenTai 导入优先检查 `Documents/JHTData/temp/db/db.sqlite`，再检查旧版本的应用支持目录。macOS/Linux 的实际安装目录可能不同，此时使用 `--jhentai-db` 明确指定。

## 查询和导出

```bash
ehentai fav categories

# 默认返回一页；next 字段可用于后续翻页
ehentai fav list --category 2
ehentai fav list --category 2 --cursor 1234567

# EH 原生搜索表达式；所有页读取完成后才返回完整结果
ehentai fav list --query 'language:chinese' --all

# 导出文件可直接用作批量操作的输入
ehentai fav list --category 2 --all --out /private/path/galleries.json

# 查询一项收藏的分组和备注
ehentai fav show https://e-hentai.org/g/1234567/abcdef1234/
```

分组编号为 **0～9**。一个画廊对应一个收藏分组；支持重命名现有分组，合并后保留源分组槽位。`remove` 仅取消在线收藏。

`list` 返回 `items`、`count`、`pages`、`next`、`complete`。默认一页时 `complete` 可能为 `false`。`--all` 检测重复游标，并默认最多请求 1000 页；达到上限会明确失败，可使用 `--max-pages` 调整。读取期间请保持收藏列表稳定，避免其他客户端同时改动导致分页遗漏。

## 添加、移动、备注和移除

下列命令支持一个或多个画廊 URL，也支持 `--input FILE`：

```bash
ehentai fav add https://e-hentai.org/g/1234567/abcdef1234/ --category 3

ehentai fav move --input /private/path/galleries.json --to-category 5

ehentai fav note --input /private/path/galleries.json --text '待阅读'

# 预览并保存操作计划；不会修改网站
ehentai fav remove --input /private/path/galleries.json --dry-run
```

语义是“设置为目标状态”，重复执行不会切换收藏开关：

| 命令 | 行为 |
| --- | --- |
| `add` | 设置目标分组；已收藏的条目也可重新归组。默认保留原备注。 |
| `move` | 移动已收藏的条目；遇到未收藏条目时，规划阶段报错。 |
| `note` | 修改已收藏条目的备注。 |
| `remove` | 移除收藏；已经未收藏的条目返回 `unchanged`。 |

输入文件可以是 `list --out` 的导出结果、JSON 数组，或包含 `items` 的对象：

```json
[
  {"url": "https://e-hentai.org/g/1234567/abcdef1234/", "category": 3, "note": "待阅读"},
  {"gid": 1234568, "token": "012345abcd", "category": 5, "note": "已阅读"}
]
```

```bash
# 按每项的 category、note 批量整理
ehentai fav add --input /private/path/items.json
# 按每项 note 修改备注
ehentai fav note --input /private/path/items.json
```

命令参数 `--category` / `--to-category` 和 `--text` / `--note` 优先于每项字段。省略备注字段会保留原备注；显式传入空字符串会清空备注。备注最多 200 个字符。JSON 中的空 `category` 不能作为添加/移动目标，必须指定目标分组。

## 原生批量移动与收藏夹合并

网站收藏页有原生批量表单：一次 `POST /favorites.php` 可提交 `ddact=favN` 和多个 `modifygids[]`。这是网站表单协议，并非公开稳定的 JSON API。CLI 为每批设置最多 50 项，写入后通过收藏列表逐项核对；仍然需要分页查询和验证请求。

```bash
# 把收藏夹 2 的内容移入收藏夹 5，目标原有内容保留
ehentai fav merge --from-category 2 --into-category 5

# 先生成合并计划，再按返回的 plan_path 执行
ehentai fav merge --from-category 2 --into-category 5 --dry-run
ehentai fav apply --plan /private/path/plan.json

# 对选中的条目使用原生批量移动
ehentai fav move --input /private/path/galleries.json --to-category 5 --native

# 每批大小可调为 1～50
ehentai fav move --input /private/path/galleries.json --to-category 5 --native --batch-size 20
```

`merge` 完整读取源收藏夹并保存快照后，分批移动到目标收藏夹。合并不会删除固定的收藏夹槽位或修改分组名称。原生移动不提交备注字段，因此会保留备注；`--native` 不接受同时修改备注，常规 `move` 仍可配合 `--text` 修改备注。

原生移动计划只记录分组变化，回滚时恢复原分组并保留当时的备注。每批先检查分组冲突，再提交一次 POST；若响应中断或部分生效，会逐项核对，报告结果未知并停止后续批次。结果记录按批次更新。重复应用计划会跳过已在目标分组的条目。

合并后会检查源收藏夹的服务器计数，返回 `source_remaining` 和 `complete_merge`。若仍有无法列出的收藏、过滤导致的缺项或其他客户端新增内容，返回 `incomplete_merge` 和退出码 5，不会把已完成的部分误报为完整合并。计划中的 `unlisted_source_count` 表示规划时服务器数量与可列出条目的差额。

## 重命名收藏夹

```bash
ehentai fav rename --category 2 --name '已阅读'

# 预览并保存可回滚计划
ehentai fav rename --category 2 --name '已阅读' --dry-run

# 同样使用通用 apply / rollback
ehentai fav apply --plan /private/path/rename-plan.json
ehentai fav rollback --plan /private/path/rename-plan.json
```

名称去除首尾空白后必须非空，长度最多 20 个表单字符（普通汉字/英文各占一个，emoji 通常占两个），不能包含控制字符。命令作用于当前 Cookie 选中的站点配置档：先读取 `uconfig.php` 当前设置，替换指定的 `favorite_N`，保留其他字段再提交；回读确认名称和其他设置。配置档变化或名称被其他客户端改动时会报告冲突。

计划仅保存配置档、原名称和新名称，不保存整份站点设置；实际提交使用执行时重新读取的最新设置。其他客户端的分组名称缓存可能需要刷新或重启后更新。

## 计划、执行与回滚

适合 AI 管理大批量收藏的流程：**完整查询 → 选择条目 → 生成计划 → 执行 → 核对结果**。不要一边翻页一边移除收藏。

```bash
ehentai fav plan move --input /private/path/galleries.json --to-category 5 --out /private/path/plan.json

ehentai fav apply --plan /private/path/plan.json --out /private/path/result.json

# 使用同一份计划核对、补做未完成项
ehentai fav apply --plan /private/path/plan.json

# 根据计划恢复原分组和备注
ehentai fav rollback --plan /private/path/plan.json
```

直接执行 `add` / `move` / `note` / `remove` / `merge` / `rename` 也会先读取目标原状态、保存计划，然后执行；返回的 `plan_path` 和 `result_path` 指向用户配置目录下的操作记录。`plan` 或 `--dry-run` 仅生成计划。普通批量操作按项保存结果，原生批量移动按批次保存结果。

计划绑定站点和账号。执行前会重新读取每项状态；如果已经达到目标状态，返回 `unchanged`；如果状态与规划时不同，则报告冲突并停止。每次写入都回读验证，POST 不自动重试。提交或验证失败时，工具会区分失败与结果未知，并停止后续操作。

结果状态包括 `updated`、`unchanged`、`failed`、`unknown`、`not_attempted`。结果文件在每项或每批处理后原子更新。批量操作不具备服务器事务能力，前面的成功操作可能已经生效；可重新应用计划或按需要回滚。

回滚恢复分组和备注。移除后再添加无法恢复服务器原始的收藏时间，按收藏时间排序时位置可能变化。若其他客户端已经编辑了条目，回滚也会检查冲突并停止。计划和结果包含收藏链接、分组与备注，默认放在用户配置目录；请妥善保存自定义导出文件。

## JSON、退出码与网络设置

业务输出默认使用易读 JSON；`--json` 输出紧凑 JSON，适合 AI 调用。`--help` / `--version` 是普通文本。

```json
{"ok": true, "data": {"authenticated": true, "site": "eh", "member_id": "123", "favorite_count": 42}}
```

```json
{"ok": false, "error": {"code": "authentication_failed", "message": "No saved login. Run 'ehentai login' first."}}
```

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功（包含无需修改） |
| 1 | 状态冲突或本地读写错误等 |
| 2 | 参数或输入格式错误 |
| 3 | 登录/访问权限错误 |
| 4 | 网络、限流或页面解析错误 |
| 5 | 批量执行中存在失败或结果未知；详情见逐项结果 |
| 130 | 用户中断 |

```bash
ehentai fav list --site ex --json

ehentai fav list --proxy http://127.0.0.1:7890

ehentai fav list --proxy socks5h://127.0.0.1:7890 --interval 2 --timeout 30
```

默认请求间隔 1 秒，串行执行。GET 最多尝试 3 次，并有有限退避；POST 只提交一次，再读取状态核对。支持系统/环境代理和显式 `--proxy`。不复用 JHenTai 的域名前置设置；需要网络访问配置时请为 CLI 配置代理。

这些接口依赖网站 HTML 和表单，网站页面改版可能需要更新解析器。认证 Cookie 也不等同于能够通过所有站点的浏览器验证。

## 开发与测试

```bash
git clone https://github.com/sounfury/ehentai-cli.git
cd ehentai-cli
python -m venv .venv
# 激活环境后
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check src tests
python -m ruff format --check src tests
python -m build
```

测试使用合成页面、虚拟 Cookie 和内存客户端，不访问真实账号。CI 检查 Windows/macOS/Linux 和 Python 3.10/3.12/3.14。

收藏请求协议参考 [JHenTai](https://github.com/jiangtian616/JHenTai)。本项目独立运行，不隶属于 E-Hentai / EXHentai 或 JHenTai。

License: [Apache-2.0](LICENSE)，参见 [NOTICE](NOTICE)。
