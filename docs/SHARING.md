# 只读观战与服务器分享

管理控制台用于创建、暂停和停止实验；观战入口只展示管理员明确选择的运行。**不要直接把没有登录系统的管理控制台或模型服务暴露给观看者。** 本文使用占位信息，不包含实际服务器地址、账号或凭据。

## 部署边界

```text
管理员 SSH → 管理控制台（服务器 loopback）→ 游戏与模型
观看者浏览器 → 独立只读 viewer → 管理控制台的受限只读投影
                                → 经管理员选定的采样录制
```

管理控制台与模型服务继续监听 `127.0.0.1`。viewer 不持有模型密钥，不创建或控制实验，不开放原始提示、原始 Trace、原生回放或任意文件下载。它只读取白名单运行的公开状态和画面；新一局不会因为启动就自动对所有观看者可见。

先按 [README](../README.md)安装并运行管理控制台、配置模型和原生游戏。管理员在源码目录检查只读入口的实际参数：

```bash
python -m sc2_viewer --help
```

使用 `RUN_ID` 指定允许展示的实际 32 位十六进制运行 ID。只有经过筛选的运行、公开说明和媒体才应加入观战列表；包含业务内容或敏感提示的历史实验不应默认展示。录制目录应与原始运行缓存分开，仅放允许分享的媒体。

以下从项目源码目录启动，仅用占位值示意；先把 `RUN_ID` 替换为获准展示的实际 ID：

```bash
RUN_ID=REPLACE_WITH_APPROVED_32_HEX_RUN_ID
python -m sc2_viewer --host 127.0.0.1 --port 8903 \
  --upstream http://127.0.0.1:18890 \
  --allow-run-id "$RUN_ID" --default-run-id "$RUN_ID"
```

当前 viewer 将上游限制为本机 `18890` 管理工作台，不接受任意远程地址；不要用它转发外网服务。需要展示多局时重复 `--allow-run-id`。新增运行须由管理员更新白名单并重启 viewer，不因此重启游戏或模型。

可选地，在独立的 `/path/to/viewer-media/RUN_ID/` 中放入获准展示的 `recording.webp`，再增加对应参数：

```bash
RUN_ID=REPLACE_WITH_APPROVED_32_HEX_RUN_ID
python -m sc2_viewer --host 127.0.0.1 --port 8903 \
  --upstream http://127.0.0.1:18890 \
  --allow-run-id "$RUN_ID" --default-run-id "$RUN_ID" \
  --recordings-root /path/to/viewer-media --recording "$RUN_ID=$RUN_ID"
```

`--recording` 的右侧是媒体根目录下的相对文件夹，不是任意文件路径。可选 `manifest.json` 只通过安全字段重新投影展示，不直接下载原文件。不要把完整 `runtime/`、提示或凭据目录当作媒体根。

## 通过 SSH 观看

推荐先让 viewer 监听服务器 loopback，端口示例为 `8903`。在观看者自己的电脑运行：

```bash
ssh -N -L 18903:127.0.0.1:8903 SSH_ACCOUNT@SERVER_LAN_IP
```

浏览器打开 `http://127.0.0.1:18903/`。`127.0.0.1` 表示当前电脑；SSH 将本地 `18903` 转发到服务器本机的 viewer `8903`。这里转发的是观战入口，不是管理控制台。`SSH_ACCOUNT`、`SERVER_LAN_IP` 及实际 SSH 端口/密钥由管理员私下提供，不写入公开仓库。

若需管理员自己使用控制台，可另开仅供管理员的转发：

```bash
ssh -N -L 18890:127.0.0.1:18890 SSH_ACCOUNT@SERVER_LAN_IP
```

## 在内网直接分享

管理员也可以只把 viewer 绑定到获准使用的 `SERVER_LAN_IP`，使用防火墙或反向代理限制访问范围；管理控制台和模型端口仍保持 loopback。此时观看地址形如 `http://SERVER_LAN_IP:8903/`。viewer 的只读性质不等同于用户身份认证：知道地址的人可能看到被列入白名单的内容，因此不要在入口中放未获准分享的实验。

需要公网访问时，应由管理员配置 HTTPS、访问认证与适当的网络限制，保持控制接口不可达；本项目文档不提供默认公网开放配置。不要在群消息或仓库中粘贴 SSH 私钥、密码或模型密钥。

## 画面与长期运行

当前运行的原生帧是最近一次实际取得的 RGB；模型等待、暂停或终局时，画面可能保持不变。历史战术视图来自快照重建，采样录制包含等待时间，都不应标为正在进行的实时对战。原生画面缺失时应显示不可用，不能用战术图冒充。

采样 `recording.webp` 仅在管理员明确配置后提供；它不是原生帧率视频。原始 `.SC2Replay`、完整 manifest、提示和任意运行文件不作为公开观战下载面。公开结果与已知限制见 [DEMO_RESULTS](DEMO_RESULTS.md)。

需要持续提供入口时，可使用服务器已有的进程管理方式维护 viewer，并定期确认上游状态、允许的运行列表和媒体容量。保持进程运行不代表游戏持续自动开局；结束的样本应明确显示终局，后续新运行仍需管理员创建和加入白名单。

仓库提供 [工作台 user unit 示例](../deploy/sc2-harness-workbench.service)与[观众 user unit 示例](../deploy/sc2-harness-viewer.service)。先修改项目路径、允许的运行 ID 和监听地址；工作台环境文件放在个人配置目录，权限设为仅本人可读，模型服务仍由维护者单独配置。不要把真实环境文件提交到 Git。

复制修改后的 unit 到 `~/.config/systemd/user/` 后执行：

```bash
systemctl --user daemon-reload
systemctl --user enable --now sc2-harness-workbench.service sc2-harness-viewer.service
systemctl --user status sc2-harness-workbench.service sc2-harness-viewer.service
```

若需要退出登录和重启后继续运行，还须由有权限的维护者为运行账号启用 linger，并确认本地模型及游戏依赖能在该账号的非交互环境使用。先停止同端口的旧进程，并确认没有活动实验，再迁移到服务管理；不要同时启动两个工作台访问同一份运行数据库。
