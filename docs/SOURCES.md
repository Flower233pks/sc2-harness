# 依赖与实现依据

本页保留实现与实验来源；组件、模型与游戏资产的权利边界另见 [THIRD_PARTY.md](THIRD_PARTY.md)。本项目不因公开源码而授予第三方资产许可。

本项目自行实现运行器、三角色策略、工具协议、证据存储和网页；未将其他星际 LLM 框架整体复制为项目代码。

| 组件 | 用途与边界 | 官方来源 |
| --- | --- | --- |
| Blizzard SC2 Linux 4.10 / Base75689 | 真实游戏进程、规则、地图、原生胜负、RGB、回放。游戏本体不随源码交付，使用受暴雪相应条款约束 | [s2client-proto](https://github.com/Blizzard/s2client-proto#downloads) |
| BurnySC2 6.6.0 | Python 原生 API 客户端、游戏数据和机械命令。MIT；适配器通过显式步进控制游戏，没有调用自动整局 Bot 策略 | [python-sc2](https://github.com/BurnySc2/python-sc2) |
| FastAPI / Uvicorn / HTTPX / Pydantic / Pillow | HTTP、WebSocket、模型接口、校验和图像编码；准确版本由安装环境记录 | [pyproject.toml](../pyproject.toml) |
| OpenAI-compatible Chat Completions | 只是一种模型 HTTP 协议；可连接公司本地服务，不要求购买 OpenAI API | [策略适配器](../sc2_harness/policies.py) |

官方 Linux 引擎包本次下载地址为 `https://blzdistsc2-a.akamaihd.net/Linux/SC2.4.10.zip`，本次实际下载 SHA-256：`1052f249f30a242bf5e17cc3315324919f9689307ca6dbcdb30bb41ea3004884`。这记录的是本次下载内容，不能代替阅读官方使用条件。

本实现只承诺一个原生玩家与一个内置电脑。BurnySC2 当前公开的两玩家 API 限制见[维护者说明](https://github.com/BurnySc2/python-sc2/issues/215#issuecomment-3012626101)。三个功能 Agent 是同一玩家内部的决策分工，不是原生 3v3 或 2v2 联盟接口。

调研过但未作为运行依赖的项目包括 [TextStarCraft2](https://github.com/sc2musa/Large-Language-Models-play-StarCraftII) 和 [Orak](https://github.com/krafton-ai/Orak)。它们的论文或演示成绩不是本项目的测试结果。
