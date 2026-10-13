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
| hx-05 Token 预算与计数 | `harness/budget.py` | `tests/test_budget.py` |
| hx-06 Prompt cache 稳定性 | `harness/prompt_cache.py` | `tests/test_prompt_cache.py` |
| hx-07 上下文压缩 | `harness/compaction.py` | `tests/test_compaction.py` |
| hx-08 工具输出卸载 | `harness/spill.py` | `tests/test_spill.py` |
| hx-09 短期与工作记忆 | `harness/working_memory.py` | `tests/test_working_memory.py` |
| hx-10 长期记忆与指令文件 | `harness/memory_store.py` | `tests/test_memory_store.py` |
| hx-11 检索与注入 | `harness/retrieval.py` | `tests/test_retrieval.py` |
| hx-12 过期、衰减与替代 | `harness/memory_lifecycle.py` | `tests/test_memory_lifecycle.py` |
| hx-13 权限规则引擎 | `harness/permissions.py` | `tests/test_permissions.py` |
| hx-14 Shell 命令解析 | `harness/shell_parse.py` | `tests/test_shell_parse.py` |
| hx-15 沙箱 | `harness/sandbox.py` | `tests/test_sandbox.py` |
| hx-16 审批与升档 | `harness/approval.py` | `tests/test_approval.py` |
| hx-17 Prompt 注入威胁模型 | `harness/injection.py` | `tests/test_injection.py` |
| hx-18 重试与退避 | `harness/retry.py` | `tests/test_retry.py` |
| hx-19 超时 | `harness/timeouts.py` | `tests/test_timeouts.py` |
| hx-20 熔断与降级 | `harness/circuit.py` | `tests/test_circuit.py` |

`tools.py` 和 `tool_runtime.py` 各有一个 `ToolSpec`：前者描述工具本身（schema、handler、只读标志），后者只描述调度需要的信息（可否并行、超时）。分开写是为了让每章的代码能单独读懂；第 25 章组装 mini-harness 时会把两者合并。

安全部分（13–17）的模块按依赖顺序串起来：`sandbox` 复用 `permissions` 的路径规范化，`approval` 包在 `permissions` 的判定外面。`sandbox.py` 是进程内的教学版，挡不住子进程自己打开的文件，`test_sandbox.py` 里的 `LimitTests` 和 `test_injection.py` 里的 `LimitTests` 专门记录这类局限：它们断言的是“局限存在”，换成真正的隔离或更强的检测之后应当改写。

韧性部分（18–20）的时钟、睡眠和随机数都可以注入，测试不真的等待（`test_timeouts.py` 里两条协作式超时的测试各约 0.1 秒）。参考仓库都没有三态熔断器，`circuit.py` 是按通用做法写的教学版。

后续章节的文件随章节补充。
