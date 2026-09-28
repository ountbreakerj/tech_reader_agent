# Tech Reader Modular

这是 Tech Reader 的源码重构版：源码按模块、章节、section、样式和资源拆分，构建后仍输出一个可离线双击打开的 `dist/tech-reader.html`。

根目录原始 `tech-reader.html` 保留在项目上一级，只用于首次导入和无损回归验证；日常不要编辑它。

## 日常工作流

给维护 Agent 的需求应描述“改什么、面向谁、期望行为”。例如：

> 在 LLM 的 MySQL 章节增加索引失效案例，放在执行计划之后，并配一张简洁的流程图。

Agent 应按以下顺序工作：

1. 先读 `agent/skills/tech-reader-maintainer/SKILL.md` 和 `catalog.json`（如已生成）。
2. 根据模块和 section 定位最小文件范围；不要读取生成产物。
3. 修改 `src/modules/**`、`src/data/**` 或 `src/assets/**`。
4. 新增内容时同步更新对应 `module.json` 的 `content` 与 `toc`；TOC 中的 anchor 必须真实存在。
5. 图片使用 `@asset/<资源ID>`，通过 `node tools/assets.mjs add <图片路径>` 登记，不手写 data URI。
6. 运行 `node build.mjs`、`node verify.mjs` 和 `node tools/assets.mjs audit`。
7. 涉及交互或视觉的修改，读取 `agent/skills/tech-reader-design-review/SKILL.md`，生成截图并写入 `reports/visual/latest/`，再输出结构化审计报告。
8. 处理审计中的 `blocking` 和 `major`；修复后最多重新审计两轮，仍有争议的项目交给用户决定。

## 命令

```text
node build.mjs                         # 生成 dist/tech-reader.html
node verify.mjs                        # 结构、资源、脚本、离线依赖校验
node tools/assets.mjs audit            # 图片登记、格式、哈希校验
node tools/assets.mjs add <file>       # 加入图片资源并打印 @asset 引用
node tools/catalog.mjs                 # 生成小型项目目录索引 catalog.json
node tools/capture-visuals.mjs         # 生成设计审计截图（需 Edge/Chrome）
node tools/learning-menu-audit.mjs     # 检查分类入口、返回、键盘和手机目录（需 Playwright/Edge）
node tools/review.mjs <report.json>    # 校验设计审计报告并执行阻断门
node tools/verify-baseline.mjs            # 确认上一级原始文件仍是迁移基线
npm test                               # 构建 + 结构/资源/浏览器审计
npm run test:baseline                  # 检查原始基线未被覆盖
npm run package                         # 验证并生成跨电脑部署压缩包
```

## 跨电脑使用与一键部署

项目同时提供源码包和无需依赖的离线阅读器。打包前会自动构建、结构验证和图片审计：

```text
pwsh -File tools/package.ps1
```

脚本默认在项目上一级生成 `tech-reader-modular-版本-时间.zip`。把压缩包复制到另一台 Windows 电脑并解压后，双击 `一键部署.cmd` 即可：

- 注册或更新当前用户的 Codex `Tech Reader Agent`，并自动写入实际解压路径；
- 打开随包提供的 `dist/tech-reader.html`，无需安装 Node、服务器或其他运行时；
- 如果电脑已有 Node.js 20+，可运行 `一键部署.cmd -Build` 从源码重建并验证产物。

脚本不需要管理员权限。以后如果把项目目录移动到新位置，再运行一次 `一键部署.cmd`，Agent 会同步到新的路径。源码维护仍遵循本 README 的日常工作流；`dist/tech-reader.html` 始终是生成产物，不要手工编辑。

CodeGraph 索引不随压缩包分发：索引包含本机路径，在另一台电脑上应按需在项目根目录重新执行 `codegraph init -i`，不要复制旧的 `.codegraph/` 目录。

审计报告按每次功能或视觉检查临时生成在 `reports/` 下，不作为源码或发布包的固定输入；涉及变化时，应生成与变更范围对应的新报告。

## 源码布局

- `src/shell/`：head、首页、公共 DOM、全局 CSS、路由和搜索。
- `src/modules/book/chapters/`：书籍章节。
- `src/modules/dsh/`、`src/modules/langgraph/`：独立模块。
- `src/modules/llm/sections/`：LLM section，每个 section 有自己的 `content.html` 和 `style.css`。
- `src/modules/*/module.json`：模块信息、顺序和 TOC 的权威来源。
- LangGraph 和 LLM 的分类入口由 `tools/lib/learning-menu.mjs` 从同一份 TOC 生成；`menu` 定义入口说明，有 `id` 的分组拥有独立目录页。新增章节时无需再维护一套入口链接。
- `src/assets/`：二进制图片及登记清单；构建时内联为 data URI。
- `src/vendor/`：第三方 Mermaid 代码，不参与普通修改。
- `design/`：设计系统、产品原则和交互规则。
- `agent/skills/`：维护 Agent 与设计审计 Agent 的可复用工作流。

## 校验原则

初始导入阶段必须保持基线 SHA-256：`e4a32352d47e39dcabfbef7d21f2f8ffa8532f5cab4b56ed66e7e1ea8c22d3ae`。日常生成模式允许因 TOC、图片外置和注册表生成而改变字节，但必须通过结构校验、脚本语法校验、资源校验和离线依赖校验。

不要为了“格式好看”重排 `<pre>`、代码示例或 SVG 内的空白；这类改动必须另行进行 DOM 和视觉回归。
