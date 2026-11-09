# 工控机部署：直接读取 DAQ 本地 DAT

当前数据路径是 `/home/ipc/disk/debug/data/both`：DAQ 直接写到工控机，监控服务也在工控机，浏览器在工控机或 Mac 打开。监控只读已有 DAT 和新增字节，不复制原始数据、不连接树莓派、不询问 SSH 密码。

## 更新与启动

先 Ctrl+C 停止旧监控。在原程序所在父目录覆盖解压新的包，保留已有 `remote.json`、`online/runtime/` 和自定义分析目录，不删除旧数据。

```sh
tar -xzf seastar-online.tar.gz
cd seastar-online
sh start.sh --check --data-dir /home/ipc/disk/debug/data/both
sh start.sh --data-dir /home/ipc/disk/debug/data/both \
  --state-dir online/runtime/local-analysis --open
```

工控机浏览器打开 `http://127.0.0.1:8766`。若旧服务占用端口，先停止它，或加 `--port 8767`。使用有权读取 DAQ 文件的普通账户，无需 sudo。

不要再传 `--remote remote.json`；旧接收功能仅在显式给出该选项时运行。旧配置文件留在目录中不会触发接收。本地数据请指向 DAQ 真正输出目录，不指向旧的 `online/runtime/received/` 镜像；含 `.receiver` 的旧镜像仍按接收事务水位保护读取。

默认从最新 DAT Run 开始，从头分析已有数据后监听追加；同一 Run 的分卷按 idx 顺序读取，默认跟随同一 FEE 的新 Run。若想固定当前 Run，加 `--no-follow-new-runs`。网页只在处理完当前 Run 可见字节后切换新 Run；一个视图展示一个 Run，可手动切换历史 Run。

## 在 Mac 查看工控机实时网页

若 Mac 与工控机处于可互通的实验室局域网，在工控机启动时加：

```sh
sh start.sh --data-dir /home/ipc/disk/debug/data/both \
  --state-dir online/runtime/local-analysis --host 0.0.0.0
```

然后在 Mac 浏览器访问 `http://工控机IP:8766`。分析和数据库仍在工控机，Mac 无需 ROOT/NumPy。服务没有登录认证，仅用于受控实验室网络；本机查看无需这个选项。

## 在 Mac 回放这一晚的文件

本目录的 `idx001.dat` 内容实际为 gzip/tar 归档，不能当作第一卷 DAT。因此这一次请使用已经能正常打开的 `data_fee_3_10261719-10270930.root` 回放，而不是把现有 15 个 `.dat` 的结果当作整晚完整统计。当前 Mac 已确认能导入所需 PyROOT。

原始文件不作改名、覆盖或解压。监控遇到 gzip 内容的 `.dat` 会明确报错停止，避免静默漏掉整卷。下面的命令明确选择 ROOT：

```sh
cd "/Users/pengyijiang/Documents/11-12月海星联调"
sh data-ana-monitor/start.sh --data-dir data_1026_1027 \
  --state-dir data-ana-monitor/online/runtime/replay-1026-1027 \
  --source data_fee_3_10261719-10270930.root --replay --open
```

Mac 浏览器打开 `http://127.0.0.1:8766`。ROOT 共有 1,182,113 个事件。已有数据会先处理积压，随后显示“回放完成”；`--replay` 不自动切换其他 Run，也不产生“没有新增数据”的取数停滞告警。完整 ROOT 文件也可在列表中单独选择，需要匹配的 PyROOT；查看 DAT 不需要 ROOT。若 DAT 数据缺卷、存在异常跳字节或结尾有半条记录，页面明确提示数据不完整，不把它称作完整回放。

首次分析会编译内核并建立指标数据库，需要一些时间和可用磁盘；图形会随分析进度更新。再次使用相同数据、参数和状态目录启动会恢复检查点。停止服务使用终端 Ctrl+C。

查看整晚趋势时，把“趋势范围”切到“当前时间段全部”，默认的“最近 300 秒”只显示末尾五分钟。横轴仍为电子学相对时间，非 UTC / 北京时间。各分布图保留独立的范围和 bin 设置。

## 依赖与文件安全

Python 3.8+、NumPy 1.19+ 和 C++11 编译器；DAT 本地模式不需要 OpenSSH、ROOT、Node.js 或 npm。Mac 已有依赖可直接运行；缺少组件时先根据 `--check` 输出补齐。Linux 可由管理员安装 `python3 python3-numpy g++`。

原始 DAT/ROOT 只读；状态、逐波形指标和编译缓存全部写入 `--state-dir`。请将状态目录和数据目录分开，并预留指标数据库空间。程序不控制 DAQ，不删除、移动、压缩或重写源数据。DAQ 尚未刷出到文件的缓冲内容需等其正常写出后才能看见。

当前使用本地直读模式，监控对树莓派没有 SSH、脚本或 SD 卡读写操作。树莓派 DAQ 自身是否还有系统日志等写入由 DAQ/系统决定。

旧的树莓派远程接收方式保留为可选功能，见 [DEPLOY_REMOTE.md](DEPLOY_REMOTE.md)。
