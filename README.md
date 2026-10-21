# FuckClassroom 课次处理插件

FuckClassroom 的独立课次处理插件，插件 ID 为 `processing`。

## 功能

- 课程课次文字处理编排
- 平台转写兜底与 PPT 文本提取
- 组合转写、OCR 和 AI 总结插件提供的服务
- 课次输出页、处理进度与结果展示

## OCR 跳过与错误诊断

「提取文字」会先检查 PPT OCR 插件是否已启用、识别服务是否已注册以及 OCR 引擎是否可用。若不能启动 OCR，原生 PPT 文本仍然提取，但任务日志和结果页会明确标明跳过原因（例如插件未启用、RapidOCR 依赖缺失、Worker 初始化异常）。

每次提取在课次输出目录写入 `ocr_status.json`（状态、引用图片数、OCR 实际处理数、识别出文字的图片数、失败数及消息），而不会因为存在 `ppt_ocr.md` 就默认显示绿色成功标签。结果页区分已识别、部分成功、识别无文字、未执行、失败、无图片、无课件等状态；较早的结果缺少状态文件时会做保守判断。

修复 OCR 插件/引擎后，点击课次结果页的「重新 OCR」即可按当前设置重新识别图片，成功后会刷新 OCR 状态；失败不删除旧结果。注意：课件图片去重和 `ocr_max_images` 限制仍生效，PPT 内图片引用数不一定等于实际推理数。

## 依赖

- FuckClassroom: `>=0.1,<0.3`
- Plugin API: `1`
- Required plugin: `classroom`
- Python dependency: `Pillow>=10.0`

插件不会直接 import `transcription`、`ocr` 或 `ai_summary` 的源码；可选能力只通过 FuckClassroom 的 ServiceContainer 查找，并通过 `PluginServiceError` 处理跨插件失败。

## 开发

开发分支为 `plugin-management`。合并到 `main` 后，CI 成功会自动发布 Registry v1 beta Release。
