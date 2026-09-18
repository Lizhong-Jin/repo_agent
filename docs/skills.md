# Skills：可复用的工作流程

[返回 README](../README.md)

本文中的命令默认在 Agent 安装目录执行；任务项目目录会单独注明。

## 使用技能

CLI 默认启用技能框架，随程序提供 `debug-and-fix`，用于定位代码报错、异常行为和失败测试，
并完成修复及回归验证。升级后先在 Agent 安装目录执行 `python -m pip install -e .`，
更新依赖并重启会话。

交互模式下使用：

```text
/skills
$debug-and-fix 修复这个项目中失败的测试
```

也可以直接描述故障，让模型根据技能描述决定是否调用 `load_skill`。
显式调用在请求模型之前加载正文；自动选择由模型决定，不保证每次相似请求都触发。
单次任务示例（单引号防止终端把 `$debug` 当作变量展开）：

```bash
repo-agent --root /path/to/project '$debug-and-fix 定位并修复启动时报错的问题'
```

只有任务开头、以空白分隔的 `$技能名` 才是显式调用，可连续指定多个名称。
正文中途或反引号中的名称不作为显式调用解析；未知的有效技能名会在请求模型前报错。
技能名使用小写字母、数字、单个连字符，不能以连字符开头或结尾，最长 64 字符。

## 编写项目技能

项目自定义技能放在**任务项目根目录**的 `skills/<技能名>/SKILL.md` 中。例如：

```text
skills/
└── diagnose-widget/
    ├── SKILL.md
    ├── references/       # 可选：按需读取的资料
    └── scripts/          # 可选：由已有隔离工具执行的脚本
```

最小 `SKILL.md`：

```markdown
---
name: diagnose-widget
description: 排查 Widget 组件的初始化失败和配置异常。
---

先查看报错及 Widget 的初始化入口，再检查配置加载顺序。
用户要求修复时，补充能复现原问题的回归验证，完成修复并报告实际验证结果。
```

采用 YAML frontmatter，支持多行描述；`name` 必须与目录同名，`description` 必须是
非空文本且不超过 1024 字符，正文不能为空。其他元数据不参与路由或权限控制。
第一版不读取 `agents/openai.yaml`、全局技能目录或 `.agents/skills`；本项目的 `.agents`
受保护且不进入沙箱副本，所以项目技能使用普通的 `skills/` 目录。

## 加载与权限

会话启动时读取并校验技能、固定正文快照，只将名称、描述与来源放入模型的技能目录。
只有显式指定或调用 `load_skill` 才把正文加入上下文；脚本和参考资料不会自动读取或执行。
内置技能随安装包分发，是纯说明技能；项目技能资源以返回的 `base_path` 为基准，
通过已有文件工具读取、隔离执行工具运行。Docker 模式从沙箱副本发现项目技能，
资源也位于同一副本；技能加载器仅返回内存快照，文件与命令操作继续走现有工具。
local 模式加载技能不会开放命令或 Python 执行权限。

名称冲突（包括与内置技能同名）、无效技能、符号链接、硬链接及非普通技能文件会明确报错，
不会静默覆盖。每个 `SKILL.md` 最多 64 KiB，总共最多 64 个技能，模型目录最多 16000 字符；
超限会提示缩短描述或减少技能。没有 `SKILL.md` 的普通子目录会被忽略。
增删或修改技能后重启会话；`/clear` 只清空对话，不重新发现技能。
已加载的正文会留在后续对话历史中，但只应在后续任务仍相关时使用。

终端会显示已加载的技能名；日志的 `skill_loaded` 事件记录名称、来源、内容 SHA-256
和加载方式（`explicit` / `model`），不复制技能正文。加载成功不等于执行成功，
技能不能扩大用户授权、改变工具权限或绕过沙箱回写流程。

## 程序调用与验证

程序调用时显式传入注册表（不传时保持原有无技能行为）：

```python
from agent import AgentRuntime
from agent.skills import SkillRegistry
from tools import create_default_tools

# client 是已经配置好的 LLM 客户端；此例为 local 工具集。
root = "/path/to/project"
runtime = AgentRuntime(client, tools=create_default_tools(root), skills=SkillRegistry(root))
result = runtime.run("$debug-and-fix 分析导入失败的原因")
```

离线验证：`python -m pytest -q tests/test_skills.py tests/test_tui.py`。
测试覆盖选择后的加载、显式调用、多轮历史、日志、目录校验、沙箱资源路径和权限保持；
模型响应使用本地模拟，不消耗推理额度，也不能证明真实模型的技能选择率。
