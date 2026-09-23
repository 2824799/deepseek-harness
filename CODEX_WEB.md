# Codex 网页端适配

改版网页运行在 3080 端口，数据存于仓库内的 .dsh-codex 目录。服务名为 codex-dsh-web、codex-dsh-appserver 和 codex-dsh-sync。升级 DSH 后可在此目录运行 node patch_apiproxy.js 重新应用安装包补丁。

项目列表来自 Codex app-server 的 project/list；同步进程把线程历史投影到网页数据目录，并按项目根路径归组。已打开页面会接收工作区新增、变更和移除事件，对话列表每两秒刷新一次；项目列表最多缓存三十秒。会话运行标记通过 Codex rollout 文件的近期写入和轮次结束事件估算，长时间没有写入的活动轮次可能暂时显示为空闲。

有些 Desktop 会话的 thread_history 索引在 rollout 继续写入后不再前进。同步进程按 ordinal 合并历史索引和 rollout 的完成事件，避免网页停在旧消息或重复显示同一事件。输入 token 按 Codex 原始用量拆成未缓存与缓存两部分；底部缓存命中率统计的是整个对话的累计输入，未必等于最近一轮的命中率。首 token 时间使用 Codex 的轮次记录，输出速度按轮次耗时扣除可识别的工具耗时估算；它是近似值，不等同于提供商逐 token 的原始速率。

模型菜单从 Codex app-server 的 model/list 读取可用模型、推理等级及默认等级。已运行的会话以最新 turn_context 为准；网页刚选模型而尚未启动下一轮时，先展示该待应用选择。

网页发送消息会请求 Codex。运行中选择“加入队列”时使用 Codex 的持久队列；选择“调整方向”时使用当前 app-server 所持有轮次的 turn/steer。Codex Desktop 持有写入锁时，独立的网页 app-server 无法调整方向或终止该轮次，网页会报告失败，不会把排队或未执行的停止操作显示为成功。网页 app-server 自己持有的运行轮次可通过 turn/interrupt 停止。

本适配的回归检查可直接运行 python3 -m pytest -q test_codex_web.py；没有安装 pytest 时，可以用标准库导入 test_codex_web.py 并调用其中的测试函数。交互测试应只使用新建的测试对话。
