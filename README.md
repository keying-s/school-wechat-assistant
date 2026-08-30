# 知序 · 学校事务助理

在 Windows 本机只读微信 4.x 数据库，把选定群聊中的通知、附件和待办整理成日程，并通过一个**独立的企业微信智能机器人**回答学校事务问题。

- 日程网站：`http://127.0.0.1:8765`
- 企业微信入口：API 模式 / 长连接，无需公网回调地址
- 学校机器人与“消息追踪”机器人使用不同 BotID/Secret，不再做跨项目问题路由
- 学校事务不会通过机器人主动提醒；只有用户主动提问时才回复

## 已实现的检索方式

问答使用“短期对话记忆 + 混合 RAG”：

1. 每位企业微信用户独立保存最近 8 轮问答，2 小时后自动结束会话上下文；
2. DeepSeek 将“这个”“1和3什么时候”等追问改写成完整问题；
3. 关键词检索优先匹配群名、文件名、课程名、编号和日期；
4. `BAAI/bge-small-zh-v1.5` 通过 FastEmbed/ONNX 在本机生成 512 维向量，用于补充语义召回；
5. 检索覆盖所选群聊的全部已入库消息、附件正文，以及未完成和已完成事项；
6. 命中聊天消息时，会一并读取前后相邻消息；
7. 回答中的关键事实使用 `[S1]`、`[S2]` 标注本地来源。

向量模型约 90MB，资料和向量都保存在本机，不调用云端 Embedding API。模型选择依据：[BGE 中文模型说明](https://huggingface.co/BAAI/bge-small-zh-v1.5)、[FastEmbed 支持模型](https://qdrant.github.io/fastembed/examples/Supported_Models/)。

## 安全边界

- 只读微信进程内存和本地数据库；
- 不切换微信窗口，不操作鼠标或键盘；
- 不实现个人微信自动发送；
- 原微信数据库以 SQLCipher `mode=ro` 和 `query_only` 打开；
- 只有网站中勾选的群聊会进入分析和检索；
- DeepSeek Key 只保存在 `config/.env.school`；
- 学校机器人凭据只保存在 `config/.env.school.bot`；
- 两个真实配置文件均被 `.gitignore` 排除，README 和示例配置不含密钥；
- 小于 50MB 的文件由微信自动下载；必要但未落盘或无法提取文字的附件会提醒手动查看；
- 附件解析不会启动 Office、执行宏或进行 OCR。

## 直接使用

正常情况下只需双击：

- `启动学校日程.bat`：启动后台并打开日程网站；
- `停止学校日程.bat`：停止网站、微信只读同步和学校机器人。

后台计划任务名称是 `SchoolAssistant`。同一个 `app.py` 会启动网站、微信只读同步、本地检索索引和学校机器人长连接，不需要再开第二个窗口。

可以向新的学校事务机器人提问：

- `今天和本周要做什么？`
- `名师讲堂有哪些课程？`
- 接着问：`1和3分别是什么时候？`
- `哪些必要附件还没下载？`
- `培养计划的选课要求是什么？`
- `清除上下文`：只清除本人的近期问答，不删除群消息、附件或待办。

## 首次安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config\.env.school.example config\.env.school
Copy-Item config\.env.school.bot.example config\.env.school.bot
```

然后填写：

- `config/.env.school`：学校项目专用 DeepSeek API Key；
- `config/.env.school.bot`：新建学校机器人得到的 BotID 和 Secret。

注册或更新开机计划任务：

```powershell
.\register_school_task.ps1
Start-ScheduledTask SchoolAssistant
```

模型首次使用时会下载到 `data/models/`；完成后会一直复用本地缓存。

## 数据更新机制

- 已关注群聊默认每 5 秒只读检查一次本地微信数据库；
- 新消息和已下载附件先进入 `data/school_assistant.sqlite3`；
- 每次提问前最多间隔 15 秒检查一次检索语料；
- 只有新增或变化的资料块会重新生成向量，不会重复索引全部资料；
- 新消息被检测后，下一次提问即可检索到；
- 已完成事项仍然可查，但不会重新显示为未完成待办。

## 本地开发和验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe app.py
```

状态接口：`http://127.0.0.1:8765/api/status`

它会显示：

- 微信只读同步状态；
- DeepSeek 是否已配置；
- 本地模型、资料块和待生成向量数量；
- 新学校机器人是否已连接。

日志位于 `logs/school_assistant.log`。日志不会记录 Bot Secret 或 DeepSeek Key，也不会完整记录用户问题正文。

## 常见问题

### 机器人第一次回答较慢

首次安装需要下载约 90MB 的本地模型并生成初始向量。以后只有新增资料需要计算，速度会明显加快。

### 找到了文件名，但没有附件内容

查看回答或网站里的附件状态：

- `missing`：文件还没有被微信下载，请在微信中手动点击下载；
- `unsupported` / `empty`：旧版 Office 文件、扫描版 PDF 或没有文字层，需要手动查看；
- `available` / `extracted`：附件正文已进入本地检索。

### 追问仍然指代错误

每位用户的上下文独立，并在 2 小时后过期。换话题前可以发送 `清除上下文`，再明确说出文件或活动名称。

### 企业微信机器人没有回复

依次检查：

1. `SchoolAssistant` 计划任务是否为 Running；
2. `/api/status` 中 `school_bot.configured` 和 `school_bot.connected` 是否为 `true`；
3. BotID/Secret 是否属于新的学校机器人，且没有被另一个进程重复连接；
4. 查看 `logs/school_assistant.log` 最后的重连错误。
