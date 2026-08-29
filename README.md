# 知序：本地微信学校事务助手

一个面向 Windows 微信 4.x 的本地只读学校事务整理工具。它读取你主动选择的微信群消息和已下载附件，用 DeepSeek 提取待办、截止日期和日程，并通过本地网站展示。

项目不会操作微信窗口、鼠标或键盘，不实现个人微信发消息，也不会通过机器人主动推送学校提醒。

> [!WARNING]
> 这是非官方工具。仅可用于你自己的设备、账号和获得授权的数据。微信升级可能改变本地数据库结构；使用前请备份重要数据并自行评估账号、隐私与合规风险。

## 功能

- 只读扫描微信进程内存，密钥仅保留在当前进程内；
- 使用 SQLCipher `mode=ro` 和 `query_only` 打开本地数据库；
- 联合读取 `message_0.db`～`message_N.db`，避免遗漏分片群聊；
- 网站内勾选关注群，未选择的群不会送入 AI 分析；
- 每 5 秒检查本地数据库/WAL 变化，增量读取新消息；
- DeepSeek API 提取行动事项、时间、优先级、附件要求和原消息证据；
- AI 语义去重：重复提醒、催办和截止时间更正会合并，不同场次或学期保持独立；
- 本地解析 PDF 文字层、DOCX、XLS/XLSX、PPTX、TXT、Markdown、CSV、JSON、XML 和 HTML；
- 不执行宏、不启动 Office、不做 OCR；无法读取的必要附件会进入人工查看清单；
- 本地月历、待办勾选、手动任务和可选浏览器通知；
- 可选接入已有问答机器人，仅在用户主动提问时回答。

## 隐私边界

| 数据/行为 | 处理方式 |
|---|---|
| 微信窗口 | 不打开、不点击、不控制 |
| 原始数据库 | 只读打开，不解密覆盖原文件 |
| 数据库密钥 | 运行时内存中使用，不写入项目文件 |
| 群消息 | 仅已勾选群进入本地缓存和 AI 分析 |
| 附件 | 只读取微信已下载到本地的文件 |
| DeepSeek | 发送已选群的必要消息/附件文本用于提取和问答 |
| Web 服务 | 默认仅监听 `127.0.0.1:8765` |
| 机器人 | 不主动发送学校提醒；只响应主动问题 |

运行数据保存在 `data/`，日志保存在 `logs/`。两者均被 `.gitignore` 排除，但仍应在发布前自行检查。

## 环境要求

- Windows 10/11 x64；
- Windows 微信（Weixin）4.x，且当前用户已经登录；
- 64 位 Python 3.11；
- DeepSeek API key；
- 建议让微信自动下载需要处理的文件。

## 快速开始

```powershell
git clone <your-repository-url>
cd school-wechat-assistant
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

编辑 `config/.env.school`：

```dotenv
DEEPSEEK_API_KEY=your-deepseek-api-key
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

启动服务：

```powershell
.\.venv\Scripts\python.exe app.py
```

打开 <http://127.0.0.1:8765>，进入“关注群聊”，勾选需要分析的群。首次勾选默认回看最近 7 天，随后只做增量处理。

## 后台自启动

```powershell
.\register_school_task.ps1
Start-ScheduledTask SchoolAssistant
```

也可以双击 `启动学校日程.bat`。移除自启动：

```powershell
.\unregister_school_task.ps1
```

## 配置

所有配置均位于不会提交的 `config/.env.school`：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `DEEPSEEK_API_KEY` | 空 | 必填，禁止提交 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 事项提取、去重与问答模型 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容 API 根地址 |
| `SCHOOL_HOST` | `127.0.0.1` | 本地网站监听地址 |
| `SCHOOL_PORT` | `8765` | 本地网站端口 |
| `WECHAT_POLL_SECONDS` | `5` | 消息变化检查间隔 |
| `GROUP_REFRESH_SECONDS` | `60` | 群列表刷新间隔 |
| `DEFAULT_LOOKBACK_DAYS` | `7` | 首次关注回看天数 |
| `MAX_INITIAL_MESSAGES` | `800` | 单群每批最大读取条数 |
| `WECHAT_DATA_ROOT` | 自动发现 | 自定义 `xwechat_files` 根目录 |
| `WCDB_TOOL_PATH` | 内置 vendor 路径 | 自定义密钥读取模块 |

## AI 语义去重

新消息提取时，模型会同时看到最近事项候选，并返回 `duplicate_task_id`：

- 同一活动的再次提醒、催办、措辞变化或补充说明会合并；
- 明确的截止时间延期/更正会更新原事项；
- 不同主题、场次、课程、学期或无更正关系的不同日期不会合并；
- 所有原消息证据都会保留在主事项中；
- 存量重复行标记为 `merged` 并指向主事项，不物理删除。

候选集合未变化时不会重复调用存量去重 API。

## 附件处理

支持：

- PDF：只提取已有文字层；
- Word：`.docx`；
- Excel：`.xls`、`.xlsx`、`.xlsm`（只读取计算结果，不运行宏）；
- PowerPoint：`.pptx`；
- 文本：`.txt`、`.md`、`.csv`、`.tsv`、`.json`、`.xml`、`.html` 等。

扫描版 PDF、旧 `.doc/.ppt`、加密文件和不支持的格式不会做 OCR 或 Office 自动化。若 AI 判断该附件是完成事项所必需的，网站会提示手动查看。

## 数据重置

完整重置会丢失网站内的群选择和完成状态。先停止服务并备份数据库：

```powershell
Stop-ScheduledTask SchoolAssistant -ErrorAction SilentlyContinue
Copy-Item data\school_assistant.sqlite3 data\school_assistant.backup.sqlite3
Remove-Item data\school_assistant.sqlite3
Start-ScheduledTask SchoolAssistant
```

不要删除微信自己的 `xwechat_files`。

## 可选：接入已有机器人

本部分代码用于接入企业微信机器人，目的是通过询问机器人，获得回答。没有企业微信管理员权限的不能使用。

也可以自行接入别的机器人。

`integrations/message-tracker/` 提供一个适配示例：

- 用户没有写明确前缀时，先由机器人项目自己的 DeepSeek 判断 `SCHOOL` 或 `TRACKER`；
- 学校问题再请求本地 `/api/qa`，因此两个项目的 API key 保持分离；
- `学校 ...` 与 `信息追踪 ...` 可以显式覆盖 AI 路由；
- 示例不包含主动提醒线程，也不调用发送接口。

详见 [集成说明](integrations/message-tracker/README.md)。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试覆盖数据库去重、存量合并、附件提取、读取辅助函数和 AI 结构化输出适配。测试使用临时文件和 Mock，不读取微信、不调用 DeepSeek、不发送消息。

准备公开仓库前，请再按 [GitHub 发布清单](PUBLISHING.md) 检查一次暂存内容。

## 常见问题

### 为什么某个群找不到？

微信会把群表分散在多个 `message_N.db`。本项目会扫描所有已验证的消息分片，并将联系人库与实际消息表做并集。若群仍未出现，请确认该群在当前电脑上至少接收过一条消息，然后等待群列表刷新。

### 为什么文件只有文件名？

微信尚未把文件下载到本地，或格式不支持。项目不会点击微信中的下载按钮，也不会做 OCR。

### 为什么第一次启动需要几秒？

项目需要只读扫描当前微信进程、验证各数据库密钥，并加载群列表。后续新消息通过数据库/WAL 签名增量检测。

### 能否用于训练模型？

项目只做检索、结构化提取和问答，不包含模型训练流程。

## 项目结构

```text
school_assistant/       后端、数据库、微信读取、DeepSeek 客户端
static/                 本地网站
tests/                  无真实账号依赖的测试
vendor/wcdb-key-tool/   MIT 许可的 Windows WCDB 密钥读取模块
integrations/           可选机器人适配示例
config/                 配置模板；真实 key 文件不会提交
app.py                  统一入口
```

## 第三方与许可

本项目使用 MIT License。vendored `wcdb-key-tool` 保留其原始 MIT 许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

发布前请阅读 [SECURITY.md](SECURITY.md)，确认没有提交 key、聊天数据库、附件文本、日志或备份。
