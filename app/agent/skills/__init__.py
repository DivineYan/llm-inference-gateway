"""内置 skill（声明式 workflow 模板）—— M3 §4.5 / §4.6。

skill = 可复用、可调度的运维流程（runbook），不是 Claude Skills。
每个 skill 是一个 builder 函数，按参数生成 Skill（步骤里的 args 为具体值，
步骤间用 {{step_id}} 引用前序输出）。
"""
from app.agent.skills.reports import SKILLS, build_skill

__all__ = ["SKILLS", "build_skill"]
