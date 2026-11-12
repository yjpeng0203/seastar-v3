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

## 3.0.2 资源保护与增量统计（2026-11-12）

- 整批 `20261109_1110` 的500个波形DAT分卷只读验收：G3 12,849,112、G4 18,487,908、G5 17,880,092条波形，共49,217,112条；带波形事件数及每个100 s计数箱与归档ROOT元数据完全一致。扫描耗时合计约74.6 s，扫描进程峰值RSS约44.7 MiB；检查的源文件size/mtime/mode/inode未变化。该验收覆盖新读取器和头部计数，不等同于把全部波形重新做一遍脉冲分析。
- 184卷合成读取器验收：追平后连续5次空闲检查打开旧卷0次；重新启动第一次核验184卷；旧卷中部被改写时停止读取。
- 默认100 s矩统计缓存与原值逐项对照，包含边缘箱、负波形偏移、all/good、分段、事务回滚、补建与实时追加交错；200,000条/10小时合成对照查询约0.722 s→0.151 s（本机数值，不能作为IPC提速倍率）。
- 查询隔离验收：相同查询只启动一个孩子；取消会结束被SQLite锁阻塞的查询；内存预算可只终止查询且保留metrics。网页替换与pagehide向后台发送对应取消标识。
- 1e8原始单位跨度的ROOT稀疏分箱验收：极端值、最大整数值独立箱、单位转换后的计数、冻结水位查询均精确；普通范围仍为TH1，ROOT渲染测试通过。独立ROOT查询实测峰值约528 MiB，耗时约0.79 s，因此ROOT预算单独设768 MiB（SQLite512 MiB）。
- 原有离线分析测试6项中5项通过，1项可选大缓存缺失跳过；稀疏ROOT新增2项通过。缺失真实历史分析目录的陈旧snapshot不再当作完整测试fixture；本批完整计数对照独立保留。

原始数据和验收SQLite/日志仅存本机runtime，不上传、不打包。工控机Linux资源保护和实际前台≤2 s更新目标需部署后验证；4/6 GiB是软件检测阈值，不能取代采集机现场资源监测。
