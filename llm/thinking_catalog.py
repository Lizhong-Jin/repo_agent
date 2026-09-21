"""内置模型思考能力：新增或调整已支持厂商的模型时，集中修改此字典。

结构：供应商规范名称 -> 模型名称元组 -> 能力配置。
同一组模型共享一份配置；单个模型也使用元组，例如 ("glm-5.3",)。
模型名称使用小写。精确名称优先于日期快照匹配。

modes: auto（接口默认）、disabled、enabled、adaptive。
efforts: 按快捷键切换顺序排列的原生强度；省略表示无强度档位。
effort_mode: 使用强度时发送的思考模式，默认 enabled。
default_effort: 显式开启思考时选用的强度；auto 仍使用服务端默认。
aliases: 用户输入的强度名称 -> 原生强度。
budget_min / budget_max: 固定思考预算范围；省略表示未声明。
history: 是否支持历史思考保留开关，默认 False，目前仅智谱适配。
dated_snapshots: 是否同时匹配 -YYYY-MM-DD / -YYYYMMDD 后缀，默认 False。

字段省略时使用 ThinkingProfile 的默认值。此文件仅保存能力规则，
用户当前选择另存于用户配置目录的 thinking.json。
"""

MODEL_THINKING_PROFILES = {
    "zhipu": {
        ("glm-5.3", "glm-5.3-flash", "glm-5.3-highspeed", "glm-5.3-flashx"): {
            "modes": ("auto", "enabled"),
            "efforts": ("low", "high", "max"),
            "history": True,
            "default_effort": "max",
            "aliases": {"minimal": "low", "medium": "high", "xhigh": "max"},
        },
        ("glm-5.2", "glm-5.2-highspeed"): {
            "modes": ("auto", "disabled", "enabled"),
            "efforts": ("high", "max"),
            "history": True,
            "default_effort": "max",
            "aliases": {"low": "high", "medium": "high", "xhigh": "max"},
        },
        ("glm-4.7", "glm-5", "glm-5.1", "glm-5-turbo"): {
            "modes": ("auto", "disabled", "enabled"),
            "history": True,
        },
    },
    "openai": {
        ("gpt-5", "gpt-5-mini", "gpt-5-nano"): {
            "modes": ("auto", "enabled"),
            "efforts": ("minimal", "low", "medium", "high"),
            "default_effort": "medium",
            "dated_snapshots": True,
        },
        ("gpt-5.1",): {
            "modes": ("auto", "disabled", "enabled"),
            "efforts": ("low", "medium", "high"),
            "dated_snapshots": True,
        },
        ("gpt-5.2",): {
            "modes": ("auto", "disabled", "enabled"),
            "efforts": ("low", "medium", "high", "xhigh"),
            "dated_snapshots": True,
        },
        ("o3", "o4-mini"): {
            "modes": ("auto", "enabled"),
            "efforts": ("low", "medium", "high"),
            "default_effort": "medium",
            "dated_snapshots": True,
        },
        ("gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"): {
            "modes": ("auto",),
            "dated_snapshots": True,
        },
    },
    "anthropic": {
        ("claude-opus-4-6",): {
            "modes": ("auto", "disabled", "adaptive"),
            "efforts": ("low", "medium", "high", "max"),
            "effort_mode": "adaptive",
            "default_effort": "high",
            "dated_snapshots": True,
        },
        ("claude-sonnet-4-6",): {
            "modes": ("auto", "disabled", "adaptive"),
            "efforts": ("low", "medium", "high"),
            "effort_mode": "adaptive",
            "default_effort": "high",
            "dated_snapshots": True,
        },
        (
            "claude-sonnet-4-5",
            "claude-haiku-4-5",
            "claude-sonnet-4-0",
            "claude-opus-4-0",
            "claude-opus-4-1",
        ): {"modes": ("auto", "disabled", "enabled"), "budget_min": 1024, "dated_snapshots": True},
    },
    "gemini": {
        ("gemini-3-pro-preview",): {
            "modes": ("auto", "enabled"),
            "efforts": ("low", "high"),
            "default_effort": "high",
        },
        ("gemini-3-flash-preview",): {
            "modes": ("auto", "enabled"),
            "efforts": ("minimal", "low", "medium", "high"),
            "default_effort": "high",
        },
        ("gemini-3.1-pro-preview",): {
            "modes": ("auto", "enabled"),
            "efforts": ("low", "medium", "high"),
            "default_effort": "high",
        },
        ("gemini-2.5-pro",): {"modes": ("auto", "enabled"), "budget_min": 128, "budget_max": 32768},
        ("gemini-2.5-flash",): {
            "modes": ("auto", "disabled", "enabled"),
            "budget_min": 1,
            "budget_max": 24576,
        },
        ("gemini-2.5-flash-lite",): {
            "modes": ("auto", "disabled", "enabled"),
            "budget_min": 512,
            "budget_max": 24576,
        },
    },
    "qwen": {
        (
            "qwen-plus",
            "qwen-flash",
            "qwen-turbo",
            "qwen3-32b",
            "qwen3-8b",
            "qwen3-14b",
            "qwen3-30b-a3b",
            "qwen3-235b-a22b",
            "qwen3.5-plus",
            "qwen3.5-flash",
            "qwen3.8-max",
            "qwen3.8-flash",
        ): {"modes": ("auto", "disabled", "enabled"), "budget_min": 1},
        (
            "qwen3-next-80b-a3b-thinking",
            "qwen3-235b-a22b-thinking-2507",
            "qwen3-30b-a3b-thinking-2507",
        ): {"modes": ("auto", "enabled"), "budget_min": 1},
    },
}
