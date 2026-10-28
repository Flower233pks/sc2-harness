# 接入团队 Harness 的边界

当前运行器用 `ScriptedPolicy` 或 `LLMPolicy` 提供三个角色的决策，尚未连接正式团队框架。环境、网页和证据模块已独立，可先运行与验证，再替换策略入口。

```text
网页 / 批量评测
      │ 创建任务、控制、读取结果
   RunManager ─────────── SQLite / Trace / 快照 / 回放
      │
      ├─ economy policy ─────┐
      ├─ production policy ──┼─ 共享模型 API 或团队 Harness adapter
      └─ tactics policy ─────┘
      │ 三份提议 → 权限、参数与共享资源校验
   NativeBackend → 隔离 worker → 真实 SC2 的一个玩家
```

替换点在 `RunManager(..., policy_factory=...)`，也可通过 `create_app(..., policy_factory=...)` 注入。适配器实现：

```python
async def decide(role: str, observation: dict, messages: list[dict]) -> dict:
    ...

async def close() -> None:
    ...
```

`decide` 收到当前可见游戏状态、明确任务目标与里程碑、该角色上一轮工具回执以及已经投递的协作消息。返回 `goal/reason/actions/messages`，按 [CONTRACTS](CONTRACTS.md) 校验。`reason` 是简短公开说明，不要求模型输出思维链。

团队 Harness 应负责规划、上下文、角色状态和通信；适配器只转换输入输出格式。不能把任意模型代码直接交给游戏进程，也不能绕过运行器同时推进同一游戏。调用模型时游戏暂停，统一汇总三个角色提议后，只有运行器推进游戏时间。

如接远程 Harness，可把 `decide` 包成 HTTP/MCP 客户端，但要保留请求超时、身份、实际调用与用量账本。当前没有实现这个远程协议，不应把本地 Qwen 服务误称为已经接通团队群智大脑。

最小接入验收：相同地图、种子、工具和预算，先跑脚本环境基线，再跑团队策略；检查实际游戏状态、原生 RGB/回放、完整决策和回执，最后比较任务结果、无效调用、耗时及 token。独立测完后，再与本项目本地模型策略比较。通信开关是消息投递消融，不等同于已经实现集中式/分布式两种团队 Harness 架构。
