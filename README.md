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

## 3.0.2：受控资源与增量统计

- 重统计全站同时运行一个独立进程，队列最多8项/16个请求；相同在途查询共用任务。修改条件、离开页面、超时均可停止后台计算。SQLite查询默认512 MiB RSS预算，ROOT查询含运行库768 MiB，时间预算90秒；Linux另设地址空间增长限制。返回包超过32 MiB时明确要求缩小详情范围，原始数据和导出保留。
- 默认100 s趋势随指标事务累计N/均值/M2，刷新复用已完成箱，边界精确计算。旧数据库分块补建缓存，取消后可续建；冻结水位保持精确，实时页面明确显示采用的最新一致水位。
- DAT封卷校验结果缓存，空闲检查不再重新打开全部旧卷；重启核验锚点，旧卷改写、替换、截短及缺卷仍告警停止。
- 全范围1原始单位/bin不变；大跨度采用ROOT稀疏对象，避免按空范围建立巨型数组。普通范围保留TH1，单位转换只改变坐标。预览先筛选显示点再转Python，详情仍查精确bins。
- 监控进程树RSS达到4 GiB时暂缓完整分析和新重查询，6 GiB保护阈值会终止正在运行的重查询；轻量计数继续。阈值是软件检测保护，不是整机物理内存的硬性保证，工控机仍需现场压测。
- 页面显示RSS、重查询PID和队列；状态目录记录有界的`resources.jsonl`、`processes.jsonl`和`queries/tasks.jsonl`。采集中不自动启动未选中的历史ROOT任务；ROOT/重查询低优先级运行，数值库线程数设为1。

升级步骤仍是停止监控（DAQ继续）→下方Git pull→启动→Ctrl+Shift+R。已有逐波形分析断点保留；首次100 s旧缓存补建有进度成本，不会重算原始波形。自定义配置路径不自动改写；若`organizer.work_dir`仍在机械盘，可在停止归档后将该目录完整迁往NVMe（例如`~/.cache/seastar-v3/organizer`），再修改配置，保留旧目录直到验证成功。

## 3.0.1：计数率优先更新

- 实时 DAT 使用独立轻量进程增量解析触发头和波形头，计数率不等待完整波形分析、质量筛选或逐波形指标写盘。三串独立计数；暂停完整分析时计数继续，启用新 Run 跟随时计数也可独立跟随。
- 默认全部已记录波形，`Hz = 箱内整数计数 / 实际观测箱长`；保留默认 100 s、自定义箱宽、连线、Poisson 灰色误差带和右侧计数率分布。100 s 是统计箱宽，当前未满箱会持续更新。
- 计数率使用独立 1 s 请求周期，正常前台运行的显示更新目标 ≤2 s；请求串行完成，不因数据增加而反复取消，等待或出错时保留上一幅图。界面显示计数时间、未读字节、缺卷/数据异常、无新增数据和计数已跟随的新 Run。
- 合格波形计数率需要完整质量分析，界面明确标注分析进度限制。ROOT 历史回放继续复用原来的分析/计数路径。
- 其他趋势每 10 s 自动更新；波形及其他面板每 30 s 更新，修改显示设置立即生效。原始 ADC 细分箱、单位换算及完整分析指标保持。
- 其他时间趋势的右侧原始值分布在放大详情时计算；缩略图不以时间箱均值冒充原始分布。

首次升级会从所选 Run 开头建立独立计数缓存，过程中显示计数未读量；之后原子保存计数和读取断点，重启续读。两秒目标需在工控机前台浏览器和正常磁盘负载下实测；初次追平、采集无新增或连接异常均显示实际状态，不伪造新数据。

## 工控机从 GitHub 下载（推荐）

仓库已公开，下载无需 GitHub 登录、验证码或访问令牌。以下命令在工控机 Ubuntu 终端执行。

首次下载：

```sh
sudo apt update
sudo apt install -y git
mkdir -p ~/SeastarMonitor_pyj
cd ~/SeastarMonitor_pyj
git -c http.version=HTTP/1.1 clone https://github.com/yjpeng0203/seastar-v3.git
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
git -c http.version=HTTP/1.1 pull --ff-only
cat VERSION
sh scripts/start_ipc.sh
```

现场配置、镜像和运行状态不纳入 Git；网页更新后按 `Ctrl+Shift+R` 刷新。HTTP/1.1 选项用于减少部分网络下的 GitHub TLS 中断，不关闭证书校验。更新命令需在带 `.git` 的克隆目录中运行；通过 ZIP/tar.gz 下载的源码目录请先另行克隆仓库，再按下方说明迁移现场配置。若需要生成可拷贝到其它工控机的离线安装包，可在项目目录执行 `python3 scripts/build_release.py`。

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
