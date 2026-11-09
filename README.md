# SeaStar v3

SeaStar 三串波形监控、FEE CSV 监控及测试结束归档。v3 汇总现有 v2 功能及修复，以独立项目发布；原 v2 和采集数据保留。

## 功能

- 多串独立后台分析、增量 DAT、分卷与新 Run 跟随、断点恢复；切换网页查看不停止其它串。
- 波形、基线、噪声、幅度、电荷、定时及质量诊断；全范围直方图，原始 ADC 单位分箱、单位换算保持箱数，线性/logY、缩略卡片及放大详情。
- 趋势默认100 s分箱并连接相邻点，可改箱宽或查看原始值；Hz计数率、统计误差阴影、共用纵轴的分布与北京时间参考。
- FEE CSV 本地/SSH镜像、自动跟随、图表缩略及详情。密码仅驻留内存。
- 网页“测试结束 · 数据归档”：选Run/串，确认停写，一键完成末卷解码、ROOT合并、原始DAT与触发旁路副本、CSV匹配、manifest及SHA256校验。原文件保持原位。
- C++/ROOT 全量离线分析，结果按 Gx/results 分类保存。
- 最终校验宏放在共享工作目录；校验失败保留生成文件并标记未验证，不把未读ROOT误报为0事件。

三串各用自身电子学时间；不进行跨串同步、符合或时空联合分析。

## 工控机从 GitHub 下载（推荐）

仓库已公开，下载无需 GitHub 登录、验证码或访问令牌。以下命令在工控机 Ubuntu 终端执行。

首次下载：

```sh
sudo apt update
sudo apt install -y git
mkdir -p ~/SeastarMonitor_pyj
cd ~/SeastarMonitor_pyj
git clone https://github.com/yjpeng0203/seastar-v3.git
cd seastar-v3
```

下载位置为 `~/SeastarMonitor_pyj/seastar-v3`。如果这个目录已经是克隆的仓库，直接使用下方更新命令，不必重复下载。

首次运行前，按下方“工控机迁移与配置”准备现场配置，并确认“环境”一节中的依赖已安装。配置完成后启动：

```sh
cd ~/SeastarMonitor_pyj/seastar-v3
sh scripts/start_ipc.sh
```

在工控机浏览器打开 CSV 监控 `http://127.0.0.1:8765`，波形监控及网页归档 `http://127.0.0.1:8766`。

后续更新：先结束旧监视服务，DAQ 采集程序保持运行，再执行：

```sh
cd ~/SeastarMonitor_pyj/seastar-v3
git pull --ff-only
sh scripts/start_ipc.sh
```

现场配置、镜像和运行状态不纳入 Git；网页更新后按 `Ctrl+Shift+R` 刷新。若需要生成可拷贝到其它工控机的离线安装包，可在项目目录执行 `python3 scripts/build_release.py`。

## 工控机迁移与配置

也可通过离线安装包迁移。本机打包：

```sh
python3 scripts/build_release.py
```

将 `dist/seastar-v3-ipc.tar.gz` 和校验文件拷贝到工控机：

```sh
cd ~/SeastarMonitor_pyj
sha256sum -c seastar-v3-ipc.tar.gz.sha256
tar -xzf seastar-v3-ipc.tar.gz
cd seastar-v3
```

沿用 v2 配置时（文件存在才复制）：

```sh
cp ../seastar-v2/config/ipc.json config/ipc.json
cp ../seastar-v2/fee-monitor/config.json fee-monitor/config.json
```

没有 organizer 配置则执行 `cp config/ipc.example.json config/ipc.json`。检查原始数据、结果、CSV及work_dir路径；绝对镜像路径可以继续使用，密码不在配置中。

结束旧监视服务，DAQ采集程序继续运行，然后启动：

```sh
sh scripts/start_ipc.sh
```

- CSV：`http://127.0.0.1:8765`
- 波形及网页归档：`http://127.0.0.1:8766`

端口由旧服务占用时先退出旧服务。v3默认使用自己的运行目录。若需沿用波形断点，停止旧服务后将原 `runtime/ipc/ana-state` 复制到 v3同路径，或独立启动时继续指定原 `--state-dir`；不要让两套服务同时写同一状态目录。归档检查点通过原配置的work_dir继续复用，原始DAT不需要复制。

## 环境

- CSV：Python 3.8+、OpenSSH（远程镜像时）。
- DAT分析：Python 3.8+、NumPy、C++编译器。
- 网页归档：现场decoder、其cfg/config.json、ROOT命令或PyROOT、hadd。默认以原始DAT目录为cwd使用 `../../build/decoder` 和 `../../cfg/config.json`，可在网页高级设置修改。
- 完整ROOT回放/离线分析：匹配当前Python的PyROOT；ROOT开发工具用于原生构建。图表库随包提供，无需Node或互联网。

```sh
sh data-ana-monitor/online/start.sh --check
python3 -m organizer check --config config/ipc.json
```

## 本机运行与验证

```sh
sh data-ana-monitor/online/start.sh --data-dir /绝对路径/DAT目录 --state-dir ./runtime/local --open
sh fee-monitor/start.sh --local --data-dir /绝对路径/CSV目录 --open
```

测试命令与结果见 `docs/V3_VALIDATION.md`。ROOT会保留全局解释器和已加载动态库状态，涉及ROOT的测试模块使用独立进程运行。真实数据验收需要本机样本，样本不随代码上传。

## 仓库与数据边界

只提交程序、示例配置、说明和测试。采集数据、镜像、密码、现场配置、数据库、运行缓存和部署压缩包不入Git。ECharts等第三方库保留原有LICENSE/NOTICE。不附加未经确认的开源许可证。
