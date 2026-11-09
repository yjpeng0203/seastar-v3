# SeaStar 本地 DAT 在线监控运行包

DAQ 已直接将 `.dat` 写到工控机。本包在工控机只读增量分析 DAQ 输出目录并提供网页，无需树莓派 SSH、密码或额外数据副本。

```sh
tar -xzf seastar-online.tar.gz
cd seastar-online
sh start.sh --check --data-dir /home/ipc/disk/debug/data/both
sh start.sh --data-dir /home/ipc/disk/debug/data/both \
  --state-dir online/runtime/local-analysis --open
```

网页地址为 `http://127.0.0.1:8766`。默认跟随同一 FEE 的新 Run；不要再带 `--remote remote.json`。历史文件在 Mac/Linux 使用 `--data-dir /历史数据目录 --replay --open`；读取完成后显示回放完成，不报取数停滞。

- **[工控机部署、Mac 查看和这一晚回放命令](online/DEPLOY_IPC.md)**
- **[分析功能、积分规则和单位](online/README.md)**
- **[可选旧版树莓派远程接收](online/DEPLOY_REMOTE.md)**
- **[实际验证记录](online/docs/VALIDATION.md)**
- **[历史 ROOT 宏说明](calculate_20260809/README.md)**

包中不含原始数据、运行状态或登录凭据。首次分析需要 Python、NumPy 和 C++ 编译器；DAT 本地模式无需 ROOT、OpenSSH、Node.js 或数据库服务。原始数据只读，指标与读取位置保存到 `--state-dir`。
