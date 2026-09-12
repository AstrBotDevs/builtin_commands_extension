# Builtin Commands Extension

这是 AstrBot 的内置指令扩展插件。

从 AstrBot `builtin_commands` 中迁出的非核心指令会放在这里维护。核心内置指令仍保留在 AstrBot 主程序中，包括：

- `/sid`
- `/stop`
- `/reset`
- `/new`
- `/dashboard_update`
- `/help`

## 提供的指令

本插件提供以下扩展指令：

- `/llm`：开启或关闭 LLM 聊天功能
- `/plugin`：插件管理
- `/op`：授权管理员
- `/deop`：取消管理员授权
- `/model`：查看或切换模型
- `/history`：查看对话记录
- `/ls`：查看对话列表
- `/groupnew`：创建群聊对话
- `/switch`：切换对话
- `/rename`：重命名当前对话
- `/del`：删除当前对话
- `/persona`：查看或切换 Persona
- `/set`：设置会话变量
- `/unset`：移除会话变量

## 说明

该插件复用 AstrBot 原有内置指令实现，目录结构与原 `astrbot/builtin_stars/builtin_commands` 保持一致：

```text
builtin_commands_extension/
├── main.py
├── metadata.yaml
└── commands/
```

如需禁用这些扩展指令，可以在 WebUI 的插件管理中停用 `builtin_commands_extension`。
