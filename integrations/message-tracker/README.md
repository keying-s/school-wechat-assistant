# 可选：接入已有问答机器人

此目录只有一个无凭据的适配示例，不包含完整机器人项目。它假设宿主项目已经提供：

- `config.py`：DeepSeek 与本地服务配置；
- `llm.py`：`chat(...)` 方法；
- `logger.py`：标准日志对象；
- 一个收到用户文本后返回字符串的处理函数。

## 配置

在宿主 `config.py` 添加：

```python
SCHOOL_ASSISTANT_URL = get("SCHOOL_ASSISTANT_URL", "http://127.0.0.1:8765")
SCHOOL_ASSISTANT_TIMEOUT = int(get("SCHOOL_ASSISTANT_TIMEOUT", "75"))
SCHOOL_BRIDGE_ENABLED = get("SCHOOL_BRIDGE_ENABLED", "true").lower() in {
    "1", "true", "yes", "on",
}
```

这里没有学校 DeepSeek key。学校 key 仍只存在学校项目的 `config/.env.school`。

## 路由

将 `school_bridge.py` 复制到宿主项目，然后在原问答入口中加入：

```python
import school_bridge

def handle(message_text):
    if config.SCHOOL_BRIDGE_ENABLED and school_bridge.is_school_query(message_text):
        return school_bridge.ask(message_text)
    return original_answer(school_bridge.clean_tracker_question(message_text))
```

行为：

- `学校 本周要做什么`：明确进入学校问答；
- `信息追踪 总结今天的资讯`：明确进入原问答；
- 没有前缀：用宿主 DeepSeek 做二分类；
- 分类调用失败：默认进入原问答；
- 不包含定时轮询、主动提醒或发送 API。

请根据宿主项目的模块名调整 import。不要把任一项目的真实 `.env` 复制到另一个项目。
