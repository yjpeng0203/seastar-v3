# v3 验证记录（2026-11-09）

本版本由当前已修复的v2源码独立生成，保留波形算法、ROOT分支及archive schema兼容标识；产品版本为3.0.0。v2及其数据、配置、缓存未覆盖。

已完成：

- 波形监控Python套件59项：58通过，1项可选的大型历史完整缓存检查因v3未复制该缓存而跳过。包含三串独立进程、暂停名额、新Run/半包、原值趋势、分箱、误差及网页归档。
- 网页归档6项：真实4 MiB样本+停采末卷，解码、hadd、CSV、SHA及源文件属性保护；旁路字节保存、多Run重名和采集变化拦截。
- organizer 5项：IPC调用、增量、归档、真实21 MiB基线（19562事件）。路径兼容4项通过。
- CSV Python 15项及CSV图表/worker检查通过。
- 波形前端4组检查通过：归档、仪表板、直方图与原值趋势。
- 最终ROOT CLI验证及离线分析测试单独运行，避免ROOT全局动态库状态相互影响。

```sh
python3 -m unittest discover -s data-ana-monitor/online/tests
python3 -m unittest discover -s fee-monitor/tests
# 下列每行分别启动一个Python进程：
python3 -m unittest discover -s tests -p test_decoder_paths.py
python3 -m unittest discover -s tests -p test_organizer.py
python3 -m unittest discover -s tests -p test_root_cli_verification.py
python3 -m unittest discover -s tests -p test_batch_analysis.py
node data-ana-monitor/online/tests/test_archive.cjs
node data-ana-monitor/online/tests/test_dashboard.cjs
node data-ana-monitor/online/tests/test_histogram_controls.cjs
node data-ana-monitor/online/tests/test_raw_trends.cjs
node fee-monitor/tests/test-chart.cjs
node fee-monitor/tests/test-worker.cjs
```

本机原始样本留在runtime，仅用于验收，不提交GitHub或打包。工控机的外部decoder程序、ROOT安装、SSH及文件路径需迁移后按README自检；本机验收不代表已在工控机部署。

## 3.0.1 计数率优先更新（2026-11-10）

- 波形监控完整 Python 套件 62 项通过；前端 4 组 Node 检查通过，另有 decoder 路径 4 项、ROOT CLI 校验 3 项通过。源码 Python 编译、Python 3.8 语法兼容检查及启动脚本语法检查通过。
- G3/G4/G5 各 4 MiB 真实 DAT 副本，共 12,108 条完整波形；独立头部计数与完整 C++ 波形分析、SQLite 计数率在 100 s 与 0.1 s 箱宽逐箱一致，Poisson 误差与曝光时长一致。
- 三串完整波形分析均暂停后追加数据，轻量计数仍继续更新，计数查询均在本机 0.5 s 内完成；新 Run 计数可先于完整分析跟随，界面明确区分两者的数据源。
- 跨卷半包、缺卷等待、空触发异常时间戳、带波形时钟回退、100 s 边界、任意箱宽、源文件更改拦截、重启后不重复计数均已覆盖。
- 前端模拟慢请求超过两秒且采集序号继续增加，确认请求不被取消、不重叠、等待时旧图保留；独立 1 s 计数定时器及其他趋势 10 s 更新策略已检查。原始值分布仅在时间趋势详情打开后请求，缩略图不替换成均值分布。
- 本机真实小样本计数查询约 2–3 ms，仅作为软件路径对照。工控机的前台显示更新间隔 ≤2 s 尚需部署后在现场磁盘和浏览器负载下验收；初次建立计数缓存会显示未读量。

新增计数测试可独立运行：

```sh
python3 -m unittest discover -s data-ana-monitor/online/tests -p test_live_rates.py -v
```

GitHub 和离线包均不包含真实样本；缺少本机真实 DAT 副本时，仅对应的可选真实数据测试跳过，合成数据的分卷、恢复与三串并发测试仍可运行。
