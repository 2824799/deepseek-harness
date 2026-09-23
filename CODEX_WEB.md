# Codex 网页端适配

改版网页运行在 3080 端口，数据存于仓库内的 .dsh-codex 目录。服务名为 codex-dsh-web、codex-dsh-appserver 和 codex-dsh-sync。升级 DSH 后可在此目录运行 node patch_apiproxy.js 重新应用安装包补丁。

项目列表来自 Codex app-server 的 project/list；同步进程把线程历史投影到网页数据目录，并按项目根路径归组。已打开页面会接收工作区新增、变更和移除事件，对话列表每两秒刷新一次；项目列表最多缓存三十秒。会话运行标记通过 Codex rollout 文件的近期写入和轮次结束事件估算，长时间没有写入的活动轮次可能暂时显示为空闲。

网页发送消息会请求 Codex。运行中选择“加入队列”时使用 Codex 的持久队列；选择“调整方向”时使用当前 app-server 所持有轮次的 turn/steer。Codex Desktop 持有写入锁时，独立的网页 app-server 无法调整方向或终止该轮次，网页会报告失败，不会把排队或未执行的停止操作显示为成功。网页 app-server 自己持有的运行轮次可通过 turn/interrupt 停止。

本适配的回归检查可直接运行 python3 -m pytest -q test_codex_web.py；没有安装 pytest 时，可以用标准库导入 test_codex_web.py 并调用其中的测试函数。交互测试应只使用新建的测试对话。
