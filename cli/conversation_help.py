"""Help shared by line-mode and full-screen conversations."""

from agent import AgentRuntime

HELP = (
    "输入任务后按回车。/help 查看帮助，/clear 清空上下文，/new [名称] 启动新会话。"
    "/compact 压缩上下文并归档原文。/ledger 查看持久化工具执行证据。"
    "TUI 执行中输入普通任务会排队，完成后按顺序执行。"
    "/continue 继续因调用上限暂停的任务，再获得 max_steps 轮预算并优先执行。"
    "/queue 查看；add 文本 添加；pause 暂停；resume 继续；"
    "edit 编号 文本 修改；remove 编号 移除；move 编号 位置 排序；"
    "clear 清空等待项；retry 编号 创建重试项。以上操作均以 /queue 开头。"
    "/rename 名称 改名；/sessions 列出会话；/switch 序号或名称 切换会话；"
    "/logs [序号] --tail 100 查看日志。"
    "/exit、/quit 或 Ctrl+D 退出并保存；下次启动默认恢复，"
    "--session 序号或名称 恢复指定会话，--new-session 启动全新会话。"
    "/model 选择供应商、模型和 API Key，保存并切换。"
    "/skills 查看技能；任务开头用 $技能名 显式指定，也可由模型按需选择。"
    "/context [auto|窗口上限token数] 查看占用、自动获取或手动设置上限。"
    "/thinking list 查看有效档位；/thinking low 等直接切换，history on 保留历史思考，reset 重置。"
    "Shift+Tab 按模型切换并记住偏好；Ctrl+T 展开/折叠思考。"
    "/thinking display collapsed|expanded|hidden 设置并保存显示偏好。"
    "Ctrl+C 取消当前输入或中断任务并暂停队列；执行中要退出可先按 Ctrl+C，再输入 /exit。"
)
RESET_NOTICE = "上下文已清空；已经执行的文件操作不会撤销。"


def describe_skills(runtime: AgentRuntime) -> str:
    skills = getattr(runtime, "skills", None)
    return skills.describe() if skills is not None else "当前未启用技能框架。"
