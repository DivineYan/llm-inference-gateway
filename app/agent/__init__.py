"""M3 Agent 编排层。

双执行模式（ReAct 诊断 / Workflow 报表）共享一套底座：
工具注册表（tools/）、状态检查点（state）、模型调用（app.model）、遥测（app.telemetry）。
"""
