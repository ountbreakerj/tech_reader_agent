# Tech Reader 项目规则

这是一个“源码模块化、交付单文件”的离线阅读器项目。

- 先读 `README.md`，再根据需求读取 `agent/skills/tech-reader-maintainer/SKILL.md`。
- 只修改 `src/`、`design/`、`agent/`、`tools/` 和配置文件；不要手改 `dist/tech-reader.html`。
- `src/vendor/` 是第三方代码，除非用户明确要求，不读、不改、不格式化。
- 每次修改后运行 `node build.mjs` 和 `node verify.mjs`。
- 涉及新增/删除/交互/样式的需求，还要运行设计审计工作流；审计报告中的 `blocking` 必须清零。
- 只在确实影响全局布局、导航、主题或响应式行为时运行完整视觉截图；局部修改只截受影响模块。
- 不要通过正则改写整份 HTML。内容、CSS、图片和 TOC 都应修改各自的小文件或 JSON。
- 跨电脑发布使用 `tools/package.ps1`；`一键部署.cmd` 和 `deploy.ps1` 是受版本控制的部署入口，可随部署需求维护。

原始基线位于项目上一级的 `../tech-reader.html`，仅用于 legacy SHA-256 回归验证。
