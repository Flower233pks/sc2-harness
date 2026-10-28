# 第三方来源与权利边界

以下列出外部依赖和获取入口。游戏、地图和模型权重单独安装；各依赖保留上游许可，本仓库尚未指定独立源码许可证。

| 组件 | 在本项目中的用途 | 官方来源及边界 |
| --- | --- | --- |
| StarCraft II / SC2 Linux | 原生游戏进程、规则、原生 RGB 和回放 | [Blizzard s2client-proto](https://github.com/Blizzard/s2client-proto#downloads)。游戏本体、地图和相关资产需另行合法取得并遵守发布者条款，不随源码提供。 |
| SC2 地图 | Simple64、Simple96、AbyssalReefLE 等完整对战环境 | 获取方式参见上述官方 API 资源入口及地图的原始发布来源；本仓库仅记录地图名/身份，不打包 `.SC2Map`。不得假定 SDK 的许可覆盖地图或美术资源。 |
| BurnySC2 / python-sc2 | 当前原生 Python SDK，固定 `burnysc2==6.6.0` | [BurnySc2/python-sc2](https://github.com/BurnySc2/python-sc2)。通过包管理器安装，具体版权与许可保留在其发行物中；本仓库未复制整套 SDK。 |
| PySC2 | SC2 智能体生态参考，不是当前原生适配器依赖 | [google-deepmind/pysc2](https://github.com/google-deepmind/pysc2)。本项目实际使用 BurnySC2，不应把两者混装或将 PySC2 的功能当作本项目已实现。 |
| Qwen3 GGUF | 可选本地模型服务使用的模型权重 | [Qwen3-8B-GGUF](https://huggingface.co/Qwen/Qwen3-8B-GGUF)、[Qwen3-4B-GGUF](https://huggingface.co/Qwen/Qwen3-4B-GGUF)。权重由可选脚本下载到本地缓存，不随源码发布；具体版本、来源和 SHA 见 [LOCAL_MODEL](LOCAL_MODEL.md)，适用许可与限制以模型卡为准。 |
| llama.cpp | 可选的本地推理服务与 CUDA 构建 | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp)。脚本固定上游版本/提交并核对下载身份；源码、二进制、构建产物与其许可由安装者另行取得，不包含在本项目源码包内。 |
| FastAPI / Starlette / Uvicorn / HTTPX / Pydantic / websockets / Pillow | HTTP、WebSocket、校验、模型协议与图像处理 | 依赖约束见 [pyproject.toml](../pyproject.toml)；各发行包附带独立许可证。安装环境中的转依赖也按各自条款处理，不由本页统一改授许可。 |
| pytest / pytest-asyncio / Ruff / setuptools / wheel | 测试、检查和构建 | 作为开发或构建依赖安装，仍保留各自上游权利声明。 |

项目网页的 HTML/CSS/JavaScript、内联图标及观战界面保存在本仓库，没有加载外部 CDN，也未把其他项目的网页整体复制进来。游戏画面中出现的角色、标识和美术仍来自游戏，不属于网页代码的独立授权范围。原生录制不是本项目对游戏资产的所有权声明。

[实现依据](SOURCES.md)还列出 TextStarCraft2、Orak 等参考资料；这些链接用于区分已有研究与本项目实现，不把它们的论文/演示成绩算作本项目成绩，也不随附它们的代码、模型或数据。

发布范围为本项目源码、文档与界面截图。`docs/images/` 中的截图来自实际观众页面，其中的 SC2 游戏画面、品牌及美术仍归相应权利人。凭据、实际服务配置、原始实验数据、游戏客户端、地图、模型权重、下载缓存及构建产物不随 Git 发布。
