# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI推理调度平台 — an LLM inference middleware (gateway) for routing, orchestrating, and protecting LLM API calls at scale. It sits between callers (online services and internal employees) and model backends (OpenAI, Claude, local models, etc.).

**Tech stack**: Python, FastAPI, asyncio, Redis, Prometheus (optional)

## Coding Rules

规则 1：编码前先思考 (Think Before Coding)
1. 明确陈述假设；
2. 不确定的地方要提问而不是靠猜；
3. 暴露权衡，列出多种方案的优缺点；
4. 如果存在更简单的方法，要予以反驳。
	
规则 2：简洁优先 (Simplicity First)
1. 只写能解决问题的最少代码；
2. 不写投机性功能；
3. 不为单次使用的代码做抽象；
4. 如果资深工程师会觉得过度复杂——简化它。
	
规则 3：外科手术式修改 (Surgical Changes)
1. 只触碰必须修改的地方；
2. 不要顺便"优化"无关的代码、注释或格式；
3. 不重构没坏的东西；匹配现有风格。
	
规则 4：目标驱动执行 (Goal-Driven Execution)
1. 定义成功标准并循环直到验证成功；
2. 不要告诉 Claude 执行步骤，而是定义"成功是什么样"，让它自己迭代；
3. 能用更少步骤达成就用更少步骤。

## Conda Environment
Conda virtual environments are in D:\Python\envs
You can run it by command: "(D:\Python\shell\condabin\conda-hook.ps1) ; (conda activate agent_project)" in Powershell.