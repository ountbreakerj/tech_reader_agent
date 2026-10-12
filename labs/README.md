# labs：Harness 模块配套代码

只用 Python 标准库，兼容 3.10+。正文里的 Python 代码块都是这里文件的原样摘录。

## 运行测试

在 `tech-reader-modular` 根目录执行：

```
py -m unittest discover -s labs/tests -t labs
```

`-t labs` 让 `labs/` 成为顶层目录，所以测试里写 `from harness.llm import ...`。测试全部离线，不发网络请求。

## 切换模型

由环境变量 `HARNESS_LLM` 决定，`harness.llm.from_env()` 读取：

| 取值 | 适配器 | 需要的环境变量 | 默认模型 |
|---|---|---|---|
| `fake`（默认） | `FakeLLM`：按脚本回放，零成本、可复现 | 无 | 无 |
| `anthropic` | `AnthropicLLM`：`POST /v1/messages`，非流式 | `ANTHROPIC_API_KEY` | `claude-opus-5-5` |
| `deepseek` | `OpenAICompatLLM`：`{base_url}/chat/completions` | `DEEPSEEK_API_KEY` | `deepseek-chat` |

`HARNESS_MODEL` 可覆盖默认模型名。缺 key 时抛 `MissingAPIKey`，报错里写明要设置的变量名。

PowerShell 示例：

```
$env:HARNESS_LLM = "anthropic"
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

## 章节与文件

| 章 | 库代码 | 测试 |
|---|---|---|
| hx-00 全景图 | `harness/messages.py`、`harness/llm.py` | `tests/test_llm.py` |
| hx-01 Agent Loop 与停止条件 | `harness/loop.py` | `tests/test_loop.py` |
| hx-02 工具定义与注册 | `harness/tools.py` | `tests/test_tools.py` |
| hx-03 执行管线与并发 | `harness/tool_runtime.py` | `tests/test_tool_runtime.py` |
| hx-04 会话日志与回放 | `harness/session_log.py` | `tests/test_session_log.py` |

`tools.py` 和 `tool_runtime.py` 各有一个 `ToolSpec`：前者描述工具本身（schema、handler、只读标志），后者只描述调度需要的信息（可否并行、超时）。分开写是为了让每章的代码能单独读懂；第 25 章组装 mini-harness 时会把两者合并。

后续章节的文件随章节补充。
