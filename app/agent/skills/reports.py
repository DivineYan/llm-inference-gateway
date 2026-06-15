"""报表类 skill —— M3 §4.5（Tier 2）。

usage_report：取用量 + 取指标 → 模型分析 → 渲染 markdown 报表。
确定式步骤 + 一个 LLM 分析步，正是 Workflow（非 ReAct）的典型场景。
"""
from app.agent.workflow import Skill, Step


def usage_report(window_seconds: int = 3600) -> Skill:
    """用量/SLA 报表：按调用方聚合用量 + 全局指标，模型给出趋势与异常点。"""
    return Skill(
        name="usage_report",
        description="用量/SLA 报表：取数→分析→出报表",
        params={"window_seconds": window_seconds},
        steps=[
            Step("s_usage", "tool", tool="query_usage",
                 args={"window_seconds": window_seconds, "group_by": "caller"}),
            Step("s_metrics", "tool", tool="query_metrics",
                 args={"window_seconds": window_seconds}),
            Step("s_analyze", "model", model="gpt",
                 prompt="根据用量 {{s_usage}} 与指标 {{s_metrics}}，"
                        "用中文写出趋势、异常点与 SLA 达成情况。"),
            Step("s_report", "tool", tool="render_report",
                 args={"title": "用量/SLA 报表", "sections": [
                     {"heading": "用量(按调用方)", "body": "{{s_usage}}"},
                     {"heading": "全局指标", "body": "{{s_metrics}}"},
                     {"heading": "分析", "body": "{{s_analyze}}"},
                 ]}),
        ],
    )


# skill 名 → builder（供 /v1/skills 发现与运行）
SKILLS = {
    "usage_report": usage_report,
}


def build_skill(name: str, **params) -> Skill | None:
    builder = SKILLS.get(name)
    return builder(**params) if builder else None
