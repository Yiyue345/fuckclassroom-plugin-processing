# FuckClassroom 课次处理插件

FuckClassroom 的独立课次处理插件，插件 ID 为 `processing`。

## 功能

- 课程课次文字处理编排
- 平台转写兜底与 PPT 文本提取
- 组合转写、OCR 和 AI 总结插件提供的服务
- 课次输出页、处理进度与结果展示

## 依赖

- FuckClassroom: `>=0.1,<0.3`
- Plugin API: `1`
- Required plugin: `classroom`
- Python dependency: `Pillow>=10.0`

插件不会直接 import `transcription`、`ocr` 或 `ai_summary` 的源码；可选能力只通过 FuckClassroom 的 ServiceContainer 查找，并通过 `PluginServiceError` 处理跨插件失败。

## 开发

开发分支为 `plugin-management`。合并到 `main` 后，CI 成功会自动发布 Registry v1 beta Release。
