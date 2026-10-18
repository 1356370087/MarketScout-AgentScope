# Python 3.14 自由线程生产采用：Codex 独立参考

这是关闭质量门禁实验的独立参考，先于 MarketScout 研究创建。Codex 单独执行两次官方域名搜索，并直接抓取官方原文；未使用项目的检索、证据提取或报告生成结果。原始页面、抓取时间和 SHA-256 保存在 `.runs/marketscout-ungated-python314-20261017/codex/`。仅将研究问题和来源范围交给项目，不向项目提供本参考或评分清单。

范围固定为 CPython 3.14 已发布系列，3.13 仅用于成熟度对照。抓取页面自报 Python 3.14.8；不据此宣称它是最新已发布补丁版。未将 `/3/` 所指向的 3.15 文档或后续 ABI 提案套用到 3.14。

## 独立参考答案

**建议先做有回退能力的小范围试点，通过依赖、正确性和性能验证后再扩大。** CPython 3.14 自由线程构建已经正式支持，结束了 3.13 的实验阶段，但仍为可选构建，并非默认。PEP 779 的阶段 II 也不意味着所有第三方库和服务都可直接迁移。[S1][S2]

官方 macOS/Windows 安装器可选装自由线程二进制；源码构建使用 `--disable-gil`。`python -VV`/`sys.version` 的 “free-threading build” 或 `sysconfig.get_config_var("Py_GIL_DISABLED") == 1` 表示构建具备能力；`sys._is_gil_enabled()` 才反映当前进程的 GIL 状态，返回 `False` 表示当前关闭。[S3][S4] 应在关键依赖导入前后都检查并记录，不能仅凭解释器名称判断实际运行模式。

未明确声明支持自由线程的 C 扩展被导入时，通常会触发警告并重新启用 GIL。自由线程构建可用 `PYTHON_GIL=0` 或 `-X gil=0` 强制关闭，`1` 强制开启；命令行优先于环境变量，`0` 仅适用于用 `--disable-gil` 构建的解释器。[S3][S5] **强制关闭只是开关，不会替不安全扩展实现同步。** 依赖未就绪时应保留有 GIL 路径或推迟迁移，不用强制开关绕过兼容性验证。

C 扩展必须专门为自由线程构建编译，制品带 `t` 区分；3.14 自由线程构建不支持 Limited C API/Stable ABI，需单独 wheel，不能把已有普通 CPython 或 `abi3` wheel 的可用性当成兼容证明。扩展的支持声明、共享全局状态、借用引用、容器访问和线程状态管理都需要审查。[S6] 官方资料无法确认本服务未列出的第三方依赖是否已适配，因此这里只给验证方法，不替它们背书。

GIL 实际关闭后，CPU 密集的 Python 线程可以利用多核；已有线程池因此值得试点，但收益取决于任务粒度、可并行比例、共享锁和内存争用。原来就在 C 内释放 GIL 的工作负载，收益也可能不同。单线程执行有额外开销，内存一般增加；官方 pyperformance 的特定硬件平均值和 PEP 提案中的历史指标都不等于本服务实测。应测量吞吐、尾延迟、CPU、RSS/峰值内存及线程数扩展曲线，不承诺线性加速。[S2][S3]

内置 dict/list/set 有内部同步，当前 3.14 官方文档还给出具体操作的线程安全保证，例如单项读取、`list.append` 的原子性；因此“内置容器完全没有保证”也不准确。[S7] 但多步检查再更新、跨对象不变量、迭代时修改和业务事务并不会因此自动原子化。对共享业务状态采用明确的锁、队列、所有权划分或不可变快照；避免多线程共享同一迭代器。正常运行不崩溃不能代替结果一致性测试。[S3][S7]

自由线程不会把同一 asyncio 事件循环中的协程自动变成多核并行：事件循环仍协作调度，一次执行一个 Task，直接运行阻塞 CPU 逻辑仍会阻塞它。需要并行时可把合适工作交给线程池或 `asyncio.to_thread`，并核验实际 GIL 状态、依赖和共享数据；这也不保证任意异步对象可跨线程使用。[S8]

工程实施分四步：①盘点和锁定 Python/依赖/wheel 版本，在真实依赖导入后验证 GIL 状态；②用同输入和多线程压力检查结果、共享状态、异常与关闭流程；③在相同硬件对比普通 3.14、有 GIL 的自由线程构建、无 GIL 构建，覆盖代表性数据、线程数、长时间内存和尾延迟；④按预先设定的正确性/性能阈值灰度，监控并保留可部署的原有环境。回退既包含 `-X gil=1` 的运行方式，也保留完整普通构建及其依赖制品；两者不是相同的性能基线。这些是工程建议，官方正式支持不保证特定业务一定收益。

## 预先固定的评分清单

各项按完整正确 1、主要正确但存在实质遗漏 0.5、缺失或错误 0 计权重。条目具体预期见本表。总分只是本次单样本人工核验的辅助量，不能代替缺陷说明；任何危险错误建议都会阻止将报告认定为可直接指导生产。不得因措辞不同于参考答案扣分。工程建议允许等价方案；非问题必需的 C API 细节和 immortalization 列表不额外设门槛。

| ID | 权重 | 满分所需内容与证据 |
| --- | ---: | --- |
| F1 | 8 | 3.13 实验、3.14 正式支持、仍可选非默认；S1、S2 |
| F2 | 5 | 获取构建的安装/源码路径，`--disable-gil` 的作用；S3 |
| F3 | 10 | 区分构建能力与实际 GIL 状态，给出对应检测方法；S3、S4 |
| F4 | 12 | 扩展导入重新启用 GIL/警告的条件；强制禁用不证明扩展安全；S3、S6 |
| F5 | 8 | `PYTHON_GIL`/`-X gil` 的 0/1 含义、构建前提、命令行优先；S5 |
| F6 | 8 | 第三方 C 扩展适配和单独构建/wheel，3.14 自由线程的 Limited API/Stable ABI 边界；S6 |
| F7 | 10 | CPU 线程多核收益的条件、不保证线性/普遍加速、验证真实工作负载；S2、S3 |
| F8 | 12 | 容器单操作安全与业务复合原子性区别、显式同步与并发正确性验证；S3、S7 |
| F9 | 8 | 单线程额外开销、内存增加与测量；不把官方/历史基准当本服务数据；S2、S3 |
| F10 | 7 | asyncio 单事件循环边界及线程卸载的适用条件；S8 |
| F11 | 8 | 依赖/正确性/性能分阶段验证、灰度与明确回退；明确其工程建议性质 |
| F12 | 4 | 至少 3 个不同官方页面；关键引用支持相邻论断，版本固定，没有虚构实测 |

危险错误对照：N1 声称 3.14 自由线程已默认；N2 以构建支持标志证明进程始终无 GIL；N3 强制关 GIL 就能让任意 C 扩展安全工作；N4 内置容器锁自动保证多步业务事务；N5 自由线程让单事件循环 CPU 协程自动多核并行；N6 普通/Stable ABI wheel 可普遍直接用于 3.14 自由线程；N7 保证 CPU 服务必然线性加速或编造本服务性能数据。

## 官方资料

- S1：[What’s New In Python 3.14：正式支持](https://docs.python.org/3.14/whatsnew/3.14.html#free-threaded-python-is-officially-supported)
- S2：[PEP 779](https://peps.python.org/pep-0779/)
- S3：[Python 3.14 free-threading howto](https://docs.python.org/3.14/howto/free-threading-python.html)
- S4：[sys._is_gil_enabled](https://docs.python.org/3.14/library/sys.html#sys._is_gil_enabled)
- S5：[命令行与环境变量 PYTHON_GIL](https://docs.python.org/3.14/using/cmdline.html#envvar-PYTHON_GIL)
- S6：[C API Extension Support for Free Threading](https://docs.python.org/3.14/howto/free-threading-extensions.html)
- S7：[Thread Safety Guarantees](https://docs.python.org/3.14/builtins/threadsafety.html)
- S8：[asyncio Task 与线程卸载](https://docs.python.org/3.14/library/asyncio-task.html)
- 成熟度对照：[Python 3.13 free-threading howto](https://docs.python.org/3.13/howto/free-threading-python.html)
