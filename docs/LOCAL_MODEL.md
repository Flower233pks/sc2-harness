# 可选的本地 Qwen 模型服务

本方案给 SC2 实验提供一个不使用个人付费 API 的模型端点。三个功能 Agent 共用一份模型权重，分别维护上下文；不训练模型。最初建立 CPU 基线，随后使用单张空闲 GPU 加速，具体配置和失败记录见下文。服务器与电力仍有资源成本。

**已验证的游戏模型配置为 Qwen3-8B Q4_K_M，单张 GPU、3 个槽位、每槽 16,384-token 上下文。** 2026-10-27 从每槽 8K 调整至 16K，模型文件不变；当时实际 `/props` 已确认三个槽位及窗口，启动显存采样峰值 11,906 MiB。历史源码 `323397…` 在 AbyssalReefLE 两胜、Simple64 一败；后续 `b5c474…` 保持模型设置、给定参考策略 revision 3 和 512 思考预算，三图各一胜、同配置 Simple64 复验一败。旧成绩不借给新版本，也不表示默认 `guidance_profile=none` 或预算为 0 的快速配置获胜。结果见 [DEMO_RESULTS](DEMO_RESULTS.md)，基础任务、长程专项及全部历史失败见 [验证记录](VERIFICATION.md)。

## 复跑当前 16K / 8B 基线

以下参数对应已验证主机上的模型服务。新机器仍需核对 Linux x64、现有 CUDA 12.9 `nvcc`、CMake/g++、空闲物理 GPU1（构建目标 SM80）和磁盘空间；未在每台新机器验证部署，也不保证重复对局必胜。在项目 Python 环境、项目根目录执行。**先结束使用该模型端点的实验，确认服务无人共用，再停止自己管理的旧服务**；不要在实验仍使用该端点时执行切换。

```bash
python3 scripts/model-service.py status
python3 scripts/model-service.py stop

# 建立基础清单与运行时，再构建 GPU 服务，最后准备 8B 权重。
python3 scripts/model-install.py
python3 scripts/model-gpu-build.py
python3 scripts/model-candidate-download.py

python3 scripts/model-service.py start --device gpu1 --model-profile 8b \
  --context-per-slot 16384 --vram-budget-mib 16384
python3 scripts/model-service.py status

export SC2H_MODEL_BASE_URL=http://127.0.0.1:18891/v1
export SC2H_MODEL_NAME=Qwen3-8B
export SC2H_MODEL_CONTEXT_TOKENS=16384
unset SC2H_MODEL_API_KEY

curl --noproxy '*' --fail http://127.0.0.1:18891/v1/models
curl --noproxy '*' --fail http://127.0.0.1:18891/props
```

基础安装器目前也准备 4B 权重及 CPU 运行时，生成后续启动器需要的 `manifest.json`；选择 `--model-profile 8b` 后实际加载候选目录中的 8B 权重。安装器和候选下载器会先核对已有文件的大小与 SHA，匹配时复用，不重复下载。GPU 构建脚本复用已有源码归档和构建目录，仍执行配置/增量构建、更新清单，不能把它当作只读检查；升级或重建前须停止自己的服务。

检查服务报告模型名 `Qwen3-8B`、3 个槽位及实际每槽 16,384 窗口。在设置上述环境变量的终端重新启动工作台，例如 `python3 -m sc2_harness serve --host 127.0.0.1 --port 18890`。已有工作台不会自动读取新环境；运行游戏时使用 README 的显式 `marine_reference_v1 + reasoning_budget_tokens=512` 配方，保留地图、seed、预算和源码身份。

容量环境变量是客户端已知配置，并非自动探测；必须与实际模型服务核对。它写入每次调用的公开配置，8K、16K 和历史未知值在验收汇总中分别分组。输入与输出共享窗口；客户端在实际用量表明输出占满窗口时，最多在原重试次数和总时限内移除旧历史，不能修复时明确报错，不追加更长的 JSON 纠错重试。服务启动器默认仍为 **CPU / 4B / 每槽 8K / GPU预算12 GiB**，当前基线必须显式给出上面的 GPU1、8B、16K 和 16 GiB 参数。服务端默认关闭思考；实验中显式请求的有限思考设置由每次 LLM 请求提供，不能只凭服务启动命令认定实际使用了 512 预算。

## 历史：2026-10-26 CPU / 4B 固定配置

下文 CPU、4B 与早期 8K 数字均是对应日期和版本的部署记录，不是当前 16K / 8B 配方的性能或胜负结果。旧样例和失败继续保留，不能用后来的胜利改写。

| 项目 | CPU 基线配置 |
| --- | --- |
| 模型 | 官方 `Qwen/Qwen3-4B-GGUF`，`Q4_K_M` |
| 推理服务 | 官方 llama.cpp `b11669`，CPU-only Linux x64 发布包 |
| API / 模型名 | `http://127.0.0.1:18891/v1` / `Qwen3-4B` |
| 计算线程 | 生成 16、批处理 16；进程绑定当前允许的最后 16 个 CPU |
| 并发 | 3 个服务槽位，HTTP 线程 3 |
| 上下文 | CPU 基线当时总计 24,576 tokens，每槽 8,192 tokens |
| 思考模式 | 服务端 `--reasoning off`、`enable_thinking=false` |
| GPU | CPU-only 发布包，`--n-gpu-layers 0`、空 `CUDA_VISIBLE_DEVICES` |

已在 Ubuntu 22.04、glibc 2.35 上运行该二进制。部署需要 Linux x64、Python 3.10+ 和可用网络；安装及生命周期脚本仅使用 Python 标准库。另一个系统的兼容性仍需检查，不能保证所有发行版直接可用。

模型文件约 2.50 GB，CPU 发布压缩包约 17.85 MB；还要预留解压文件、日志与运行内存。当时小规模验证后的进程 RSS 约 8.1 GiB，包含模型与三槽上下文等开销，不等同于权重文件大小。安装器要求下载后磁盘仍有至少 20 GiB；启动器也检查此门限。

## 官方来源与完整校验值

模型的原始发布身份来自 [Qwen 官方 Hugging Face 文件](https://huggingface.co/Qwen/Qwen3-4B-GGUF/blob/a9a60d009fa7ff9606305047c2bf77ac25dbec49/Qwen3-4B-Q4_K_M.gguf)，原始 [LFS 指针](https://huggingface.co/Qwen/Qwen3-4B-GGUF/raw/a9a60d009fa7ff9606305047c2bf77ac25dbec49/Qwen3-4B-Q4_K_M.gguf) 提供大小和 SHA-256。

- 文件：`Qwen3-4B-Q4_K_M.gguf`
- 大小：`2497280256` bytes
- SHA-256：`7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`
- 固定原始下载：[Hugging Face](https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/a9a60d009fa7ff9606305047c2bf77ac25dbec49/Qwen3-4B-Q4_K_M.gguf)
- 默认下载：[Qwen 官方 ModelScope 镜像](https://modelscope.cn/models/Qwen/Qwen3-4B-GGUF/resolve/master/Qwen3-4B-Q4_K_M.gguf)。镜像分支可变，因此最终必须匹配上面的固定大小和 SHA，匹配失败即停止。

推理二进制来自 [llama.cpp 官方 b11669 发布](https://github.com/ggml-org/llama.cpp/releases/tag/b11669)：

- 文件：`llama-b11669-bin-ubuntu-x64.tar.gz`
- 大小：`17851611` bytes
- SHA-256：`cf07c1c112d709bd67e522017471de57ed0ec8bea031d732c5422351c07f65ac`
- [固定下载链接](https://github.com/ggml-org/llama.cpp/releases/download/b11669/llama-b11669-bin-ubuntu-x64.tar.gz)

## 安装与文件布局

在项目根目录执行，不需要 sudo，也不修改 ROS、Gazebo 或系统 Python 环境：

```bash
python3 scripts/model-install.py
```

默认从 ModelScope 下载模型，并与 Hugging Face 官方固定身份交叉核验。如需直接使用 Hugging Face：

```bash
python3 scripts/model-install.py --model-source huggingface
```

脚本会校验现有完整下载文件，匹配时复用；下载到临时 `.part`，大小与 SHA 全部通过后才改为正式文件名。解压前检查路径、链接及包大小，拒绝越界。升级或重新解压前先停止个人模型服务，避免改写运行中的二进制。

默认文件均在个人缓存内，权重和运行数据不要提交到 Git：

```text
~/.cache/sc2-harness/model/
  Qwen3-4B-Q4_K_M.gguf
  llama-b11669-bin-ubuntu-x64.tar.gz
  llama-b11669-cpu/llama-b11669/llama-server
  llama-b11669-cpu/llama-b11669/...
  manifest.json
  service.json
  service.log
```

可在安装后独立核对：

```bash
sha256sum ~/.cache/sc2-harness/model/Qwen3-4B-Q4_K_M.gguf
sha256sum ~/.cache/sc2-harness/model/llama-b11669-bin-ubuntu-x64.tar.gz
stat -c '%n %s' ~/.cache/sc2-harness/model/Qwen3-4B-Q4_K_M.gguf
stat -c '%n %s' ~/.cache/sc2-harness/model/llama-b11669-bin-ubuntu-x64.tar.gz
```

## 启动、状态与停止

以下展示历史 CPU / 4B / 8K 配置的生命周期接口；当前 GPU / 8B / 16K 启动请使用顶部完整参数。

```bash
python3 scripts/model-service.py start
python3 scripts/model-service.py status
curl --fail http://127.0.0.1:18891/v1/models
python3 scripts/model-service.py stop
```

启动前检查端口和磁盘；已运行的同一服务会复用。`service.json` 保存 PID、进程启动 ticks、二进制路径和参数。停止时核对这些身份与进程属主，匹配后才发送 SIGTERM；不会使用模糊进程名批量结束其他任务。

仅监听回环地址，没有开放公司内网或公网端口。网页后端与模型服务在同一服务器时可直接访问；浏览器不需要持有模型凭据或直接调用该端点。

## 接入本项目

在启动 SC2 网页后端的同一环境里配置：

```bash
export SC2H_MODEL_BASE_URL=http://127.0.0.1:18891/v1
export SC2H_MODEL_NAME=Qwen3-8B
export SC2H_MODEL_CONTEXT_TOKENS=16384
unset SC2H_MODEL_API_KEY
```

上面是当前 8B / 16K 客户端配置；只复跑历史 4B / 8K 样例时，才相应使用 `Qwen3-4B` 与 `8192`，并与实际服务核对。然后按 README 启动项目，在实验配置中明确选择 `policy=llm`。选择 `scripted` 时运行的是脚本基线，不会因为本地模型已启动就自动变成 LLM。

`LLMPolicy` 默认发送角色专属 JSON Schema，限制工具、参数形状和消息数量；同时提示工具成本、机械前提、任务目标、最近回执。游戏状态与执行层仍负责资源和规则校验，结构合法不代表动作被引擎接受，更不代表任务完成。显式指定 `strict_schema=False` 可对仅支持 JSON object 的兼容服务降级格式约束；不会自动切换成脚本策略。

## 验证与当前边界

安装好项目 Python 依赖后，可以在不启动游戏的情况下验证三角色 API：

```bash
python scripts/model-smoke.py \
  --policy-file sc2_harness/policies.py \
  --output runtime/model-smoke.json
```

该脚本使用明确标记的观察样例，并行请求三个角色，再做一条短 JSON Schema 检查。它验证模型端点、输出合同和计量；不执行游戏动作，不作为 SC2 任务成功证据。

2026-10-26 一轮实际 CPU 本地调用，在工具前提说明与角色 JSON Schema 生效后得到：

| 角色 | 验证通过的动作提议 | 输入 / 输出 tokens | 延迟 |
| --- | --- | --- | --- |
| economy | `train_worker` | 770 / 77 | 7.648 秒 |
| production | `train_marine` | 772 / 66 | 7.399 秒 |
| tactics | `scout(unit_tag=3)` | 723 / 58 | 7.195 秒 |

三个请求并行总耗时 7.648 秒，各一次 HTTP 尝试；额外短 JSON Schema 检查 0.447 秒、31 / 17 tokens。用量来自服务实际返回，不是字符数估算。最初仅列工具名的版本曾出现未通过输出合同的结果和缺少前提的研究请求，不能当作可靠的最终配置。

这是单个短观察样例的一轮结果，不是通用性能基准。它不证明长上下文、连续多局、战术质量、胜率或直播帧率；上下文增长后的延迟、槽位竞争、取消与超时，以及真实 SC2 闭环需另外验收。每槽 8,192 tokens 包含输入与输出，不能无限累积历史。

## 历史：CPU / 4B / 8K 真实对局中暴露的问题

随后的一次原生 SC2 试运行未通过：运行到第 8 轮，游戏内约 35 秒；现实经过 227.17 秒（含人工暂停），记录 24 次模型 HTTP 尝试、13 次决策错误，仅 4 条命令获引擎接受。该轮所记录的决策延迟 p50 约 21.03 秒、p95 约 29.84 秒。第 4 轮三个角色均超时；后续又出现服务 4xx，结合上下文增长判断与每槽 8k 容量有关，但不能将所有 4xx 无证据地归为同一原因。

该轮包含未返回 usage 的调用，因此累计 token 保持未知 `null`，不能按 0 计量。还观察到模型错误描述已有建筑或侦察进度；以原生状态与任务判据为准，公开理由不是完成证据。前面的短样例成功不能覆盖这些失败。

当时针对输入增长，新增了历史摘要和角色观察裁剪，不重复保存旧的全量单位、地图和任务说明；单位样本上限为 48，并标明模型输入投影，后端和网页原始观察保持完整。该修改与后续试运行分开记录，不能追溯改变旧局结果；当前有界输入、阶段交接及持续职责合同见 [CONTRACTS](CONTRACTS.md)。

摘要策略的后续一次 CPU 原生试运行由人工停止：第 25 轮、游戏内约 120 秒，现实耗时 534.35 秒；75 次模型 HTTP 尝试、0 次决策错误，实际报告输入 194,158 / 输出 5,572 tokens，决策延迟 p50 21.77 秒 / p95 24.10 秒。工人、供给和侦察三项判据通过，但模型反复建供给设施、挤占兵营资源，任务未完整通过。该样本错误更少，不足以证明通用稳定性或策略质量达标。

## 历史：4B / 8K 的 GPU1 加速与 Ubuntu 22.04 兼容性

如果已有空闲 A800，可复用同一个 GGUF；不必下载第二份权重或安装 PyTorch。模型服务与 SC2 渲染应使用不同的空闲 GPU。

官方同版本 CUDA 12.8 发布包约 172.53 MB，已核验的 SHA 为 `4703ed1a3cd6e8504affb6cb9866f501e96d634d68a002dc9a8251c9e2819b49`，来源为 [官方 CUDA 12.8 发布资产](https://github.com/ggml-org/llama.cpp/releases/download/b11669/llama-b11669-bin-ubuntu-cuda-12.8-x64.tar.gz)。但是该包实际要求 `GLIBC_2.38` 和 `GLIBCXX_3.4.32`，不能在本次 glibc 2.35 主机直接运行；**不要为它升级系统 glibc、驱动或 CUDA**。

主机已提供 `/usr/local/cuda-12.9/bin/nvcc`、CMake 和 g++ 时，可在个人缓存编译同一 [官方固定 commit](https://github.com/ggml-org/llama.cpp/tree/620d2225cc436ab0b001e3200e590e3b286ef9ab)：

```bash
python3 scripts/model-gpu-build.py
```

构建限定 `CMAKE_CUDA_ARCHITECTURES=80`、并行 8，仅生成 `llama-server` 及所需本地库，不执行系统安装。源码固定 commit，构建记录源码归档与最终二进制的本地 SHA；这些本地记录不能冒充官方发布资产的 SHA。源码、构建结果和日志都保留在 `~/.cache/sc2-harness/model/`，`gpu-release.json` 指向最终二进制。

本次源码归档大小 `38195486` bytes，本地记录 SHA-256 为 `1d4a987d9bd048f6e93107e838026438df70fbde237fc0c69c63d600512b4bd9`；编译后 `llama-server` 的本地 SHA-256 为 `7dc08e1e3bc7584f8bbfd92fdf2c11560522ebb34ab46d2a315455adc83f838d`。后者依赖编译环境，重新构建不保证相同。归档不含 `.git`，程序版本显示 `build 0, commit unknown`；固定源码 URL、归档校验值与构建清单用于补足来源记录，不能仅凭该版本字符串判断来源。

在实验已停止且 GPU1 空闲时切换：

```bash
python3 scripts/model-service.py stop
python3 scripts/model-service.py start --device gpu1
python3 scripts/model-service.py status
```

GPU 模式限定 `CUDA_VISIBLE_DEVICES=1` 和单卡分配；不会自动选择其他 GPU。启动前检查 GPU1 占用，启动时监测本服务显存，超过所选预算即停止经身份核验的本服务；默认预算 12 GiB，当前 16K 实验显式设为 16 GiB。该检查不等同于 GPU 硬件资源隔离或整个生命周期的硬上限。输入与上下文容量仍需控制。API 地址、模型名、三个角色状态和原始证据约定保持一致。

私有 CUDA 构建切到 GPU1 后，一次短观察样例的 API 实测为：

| 角色 | 输入 / 输出 tokens | 延迟 | HTTP 尝试 |
| --- | --- | --- | --- |
| economy | 884 / 64 | 0.960 秒 | 1 |
| production | 886 / 56 | 0.917 秒 | 1 |
| tactics | 837 / 66 | 0.960 秒 | 1 |

三角色并行总耗时 0.960 秒，均通过输出合同；额外短 JSON Schema 检查 0.231 秒、31 / 17 tokens。期间 8 次本进程显存采样的峰值为 6,362 MiB，约 6.21 GiB，未超过启动预算。采样峰值不是连续硬件级峰值证明，也不等于长局显存上限。

该样例中仍有冗余建造提议；它没有验证角色策略或游戏任务成功。CPU 与 GPU 样例的提示内容略有不同，不能据此宣称严格对照下的固定加速倍数。后续原生游戏应单独报告引擎接受、观察结果与任务结果，保留已有失败记录。

同日原始 4B 模型的一局 GPU 原生实验达到 60 轮、游戏内 300 秒，记录 180 次 HTTP 尝试、0 次决策错误；工人、供给、兵营、侦察四项判据通过，四名就绪陆战队员的目标未通过，任务结果为未完成。模型存在重复建造供给设施和采气设施的问题。API 稳定与模型策略有效是两项不同的验证。

## 历史：2026-10-26 官方 8B / 8K 对照候选

当时缓存了 [Qwen 官方 Qwen3-8B-GGUF](https://huggingface.co/Qwen/Qwen3-8B-GGUF) 的 `Q4_K_M`，用于保持策略和提示不变的模型更换对照。这一阶段只完成同一 GPU1 上的 API 短验证，不能从模型规模推断完整游戏成绩；后续 16K 与参考策略的真实任务结果见本文顶部和验证记录。

- 文件：`Qwen3-8B-Q4_K_M.gguf`
- 大小：`5027783488` bytes
- SHA-256：`d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`
- [固定官方文件与 LFS 身份](https://huggingface.co/Qwen/Qwen3-8B-GGUF/blob/6a569868d07d3bd59e8b97fb001bf8c0b254bb20/Qwen3-8B-Q4_K_M.gguf)
- [固定原始下载](https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/6a569868d07d3bd59e8b97fb001bf8c0b254bb20/Qwen3-8B-Q4_K_M.gguf)
- [Qwen 官方 ModelScope 镜像](https://modelscope.cn/models/Qwen/Qwen3-8B-GGUF/resolve/master/Qwen3-8B-Q4_K_M.gguf)，最终仍须匹配上述固定大小与 SHA。

在已安装原 4B 基线的个人缓存中执行：

```bash
python3 scripts/model-candidate-download.py
sha256sum ~/.cache/sc2-harness/model/candidates/Qwen3-8B-Q4_K_M.gguf
```

当前切换到 8B 请只使用顶部完整的 **16K 复跑配方**，避免漏传参数而回到历史默认 8K。启动器核验权重大小和 SHA，并复用私有 CUDA 构建。服务模型名必须同步为 `Qwen3-8B`，重启工作台后再确认新实验记录的身份。可选短样例验证支持 `model-smoke.py --model Qwen3-8B`；它不执行游戏，不作为胜利证据。

2026-10-26 切换后，`/v1/models` 和实际响应均报告 `Qwen3-8B`。短 JSON Schema 请求得到实际输入 29 / 输出 15 tokens、138.286 毫秒；随后使用与原 GPU 样例相同的观察样例和策略，三角色结果为：

| 角色 | 验证通过的动作提议 | 输入 / 输出 tokens | 延迟 |
| --- | --- | --- | --- |
| economy | `train_worker` | 884 / 69 | 1.345 秒 |
| production | `build_engineering_bay` | 886 / 65 | 1.298 秒 |
| tactics | `scout(unit_tag=3)` | 837 / 60 | 1.290 秒 |

各一次真实 HTTP 尝试，三角色并行总耗时 1.345 秒。期间 16 次显存采样峰值为 8,458 MiB，约 8.26 GiB，低于 12 GiB 启动预算；磁盘仍余约 29.35 GiB。该样例中 production 仍提出无关建造，因此只能确认端点、输出合同和资源可用，不能宣称策略质量优于原 4B 或 SC2 任务已经通过。

另外，当时调研了 [官方 Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)，它是仅支持非思考的后训练版本，官方报告其工具使用等能力改善。但当时未找到 Qwen 官方发布的该版本 GGUF；不能将社区量化版本称为官方 GGUF。调研记录中的官方 Safetensors 原权重约 8.06 GB，转换还需要额外空间与步骤。当时先使用已有官方 GGUF 的 8B 候选，没有据模型规模预判 SC2 结果；这段历史调研不代表已重新核查当前可用发布。
