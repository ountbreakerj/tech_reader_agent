# Harness 章节写作约定

适用于 `src/modules/harness/chapters/hx-NN/` 和 `labs/`。子代理只按本约定写草稿，集成由主会话用 `py tools/harness-labs.py integrate` 完成。

## 钉住的仓库（引用必须基于这些 commit）

| 简称 | 本地路径（相对 `C:\Users\j\Desktop\作业\LLM\`） | commit |
|---|---|---|
| `dsh` | `DSH/deepseek-harness` | `d743267388641bc76f17c45ce8b4c231aed1d32c` |
| `oh` | `01-agent-harness/OpenHarness` | `9b2efd795c6aa09f88b0c257d269a9e518da6ae7` |
| `lcc` | `02-claude-code/learn-claude-code` | `ce8f9f186058939da54c9d6fead78dfb5d0fd6c3` |
| `amt` | `03-agent-memory/Agent_Memory_Techniques` | `4767c0341ccd8b714147334c5f17c26ff99d8e41` |
| `lhe` | `01-agent-harness/learn-harness-engineering` | `38ddcd2bf8d65271f668b94e7c875ca1d629d622` |

`LLM/` 下的三份旧报告（README、评估报告、DSH 更新记录）有错误，不要引用，只以源码为准。

## 每章验收（四项缺一不可）

1. 源码级机制：讲清楚“它在哪一行做了什么、为什么这样做”，每个结论配 file:line。
2. 可运行 Python：正文代码必须是 `labs/` 里真实文件的原样摘录，只用标准库，兼容 Python 3.10+。
3. 坑与失败案例：至少 3 条，写清触发条件、后果、源码里怎么防的（或没防）。
4. 多仓库取舍对比表：同一机制在各仓库的做法、代价和适用场景。

示意数字（阈值、耗时、比例）不是源码里的值时，必须在旁边标“教学示意”。

## 草稿文件格式

草稿是一个 HTML 片段 `hx-NN.html`，只写章节正文，不写外层 `<section>`、章号和 `<h1>`。第一行写章标题注释：

```html
<!-- title: Agent Loop 与停止条件 -->
```

可用标签与写法（与 book 模块一致）：

- 节标题：`<h2 id="hx-NN-s1">…</h2>`，小节：`<h3 id="hx-NN-s1-1">…</h3>`。id 必须唯一，按顺序编号。
- 段落 `<p>`、列表 `<ul>/<ol>`、强调 `<strong>`、行内代码 `<code class="inl">`。草稿是纯 HTML，**不要写 Markdown**：正文里的 `**粗体**` 或反引号代码会原样显示给读者，集成时会直接报错。
- 代码块与行内代码里的 `<`、`>`、`&` 必须转义成 `&lt;` `&gt;` `&amp;`（TypeScript 泛型 `Promise<T>` 不转义会被浏览器当成标签，后面整段变成删除线）。非 Python 代码块集成时会自动转义，正文其他位置仍要自己转义。
- 同一章里引用的后续章节如果还没写，照常写 `<a href="#hx-NN">`，集成时会自动降级成纯文本，那一章集成后再自动恢复成链接。
- 提示框：`<blockquote><p><strong>标签</strong>：…</p></blockquote>`。不用自定义卡片或彩色框。
- 表格：`<div class="table-wrap"><table><thead><tr>…</tr></thead><tbody><tr>…</tr></tbody></table></div>`，每个单元格都必须在 `<tr>` 内。
- 源码引用（会被逐条机器核对）：

  ```html
  <code class="inl src-ref" data-src="dsh:packages/core/agent-loop/src/loop.ts:120" data-expect="while (true)">loop.ts:120</code>
  ```

  `data-expect` 是该行里一定出现的一小段原文（逐字、区分大小写，HTML 转义），用来防止行号漂移。行号从 1 开始。范围写 `:120-134`，`data-expect` 取首行。
- Python 代码块（必须对应 labs 文件，会被机器核对一致性）：

  ```html
  <div class="codeblock" data-lang="python"><pre><code class="lang-python" data-lab="labs/harness/loop.py::run_agent">…原样代码，&lt; &gt; &amp; 转义…</code></pre></div>
  ```

  `data-lab` 取 `文件::函数或类名`（摘录该定义的完整源码，含装饰器）、`文件::类名.方法名`（方法摘录保留原缩进），或只写文件路径（摘录整个文件）。不要手工加高亮 span，集成时自动加。
- 其他语言的源码摘录（TS 等）用 `data-lang="typescript"`，并且紧跟一个 src-ref 标明出处；摘录要逐字，可以用 `// …` 省略中间行。其他仓库的 Python 源码（如 OpenHarness）也一样：写 `<code class="lang-python">` 但**不带** `data-lab`，紧跟 src-ref；`data-lab` 只用于本仓库 `labs/` 里的代码。`data-lab` 也可以指模块级常量（例如提示词模板 `SUMMARY_INSTRUCTION`）。
- 站内链接：本模块用 `#hx-NN-s1`，跨模块用 `#/dsh?section=…`、`#/codex-harness?section=…`、`#/book?section=…`。

## labs 约定

- 目录：`labs/harness/`（库代码）、`labs/tests/`（unittest）、`labs/sandbox/`（Docker 实操）。
- 只用标准库。默认用 `FakeLLM`（脚本化回复，测试可复现）；真实模型适配器读环境变量，缺 key 时给出清楚的报错而不是崩溃。
- 每个模块配测试：`py -m unittest discover -s labs/tests -t labs`，从仓库根目录运行。
- 代码风格：类型注解、简短中文注释（解释为什么，不复述代码）、函数短小，方便在正文里整段摘录。
