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
