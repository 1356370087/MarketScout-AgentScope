# Kubernetes 原生 Sidecar 迁移：Codex 独立研究

研究时间：2026-10-16。本文在读取项目本次研究输出之前完成，依据独立获取的 Kubernetes 官方资料。研究范围是声明、生命周期、资源规则及迁移验证，不包含实际 Kubernetes 集群测试。

建议在支持原生 Sidecar 的集群迁移日志收集容器：它能明确启动与终止顺序，并避免传统常驻 Sidecar 阻塞 Job 完成。但迁移本身不能保证日志无损，也不能把容器启动成功当作依赖服务已经可用。

1. **声明与版本。** 将日志容器放入 `spec.initContainers`，并在该容器上设置 `restartPolicy: Always`。这是容器级策略；Deployment 的 Pod 策略通常为 `Always`，Job 的 Pod 策略为 `Never` 或 `OnFailure`，两者不矛盾。功能在 v1.28 为 Alpha、v1.29 默认启用、v1.33 稳定。旧集群需要同时检查 API Server、节点版本和功能门控；v1.28 的终止行为不同。还要核对准入 Webhook 是否删掉了不认识的容器级字段。[1][2]

2. **启动与就绪。** 普通 init container 要成功完成才能继续；原生 Sidecar 会持续运行，达到 `started=true` 后即可启动后续 init，随后启动应用。没有 `startupProbe` 时，进程已经运行即可达到该状态；有探针时须等探针成功。`readinessProbe` 决定 Pod 是否就绪，不是后续容器的启动屏障。官方专题实测中，Sidecar 的 readiness 永远失败，主应用仍然启动。若应用必须等日志服务可用，应让 Sidecar 的 startupProbe 检查真实可用条件，同时保留运行期间的 readiness/liveness 检查；这属于配置建议，不能用一次探针成功保证未来持续可用。[1][3][4]

3. **Job 与退出。** 原生 Sidecar 不阻止主容器结束后的 Job 完成，即使其容器策略为 `Always`；普通 `spec.containers` 中的常驻 Sidecar 不能直接套用这一规则。正常 Pod 终止时，kubelet 等所有主应用容器结束，再按声明的逆序终止 Sidecar。整个 Pod 的终止宽限期是有限的，主应用或 preStop 耗时过长，会压缩 Sidecar 的退出时间；剩余容器可能被强制终止。因此日志送达仍需要收集器的缓冲、确认、重试与持久化设计，并验证退出耗时。这些是工程建议，不是 Kubernetes 的无损承诺。[1][2][5]

4. **资源计算。** 对 CPU、内存分别核对 requests 与 limits。运行阶段须计入“所有应用 + 所有常驻 Sidecar”；初始化阶段须考虑当时同时存活的容器，包含已经启动的 Sidecar。概念上可记为 `有效资源 = Pod overhead + max(运行阶段合计, 初始化阶段峰值)`。普通 init 依次执行，不能把全部 init 和应用简单相加；也不能遗漏常驻 Sidecar。官方资源段落给出了取较大值、加入 Sidecar 和 overhead 的规则；这里按并发阶段展开，是结合生命周期作出的计算说明。实际准入默认值、未设置 limit 的情况和目标版本的 Pod 级资源配置需要另行核对，不能把未设置 limit 当作零。[1][3][6]

下面是基于官方示例的最小 Job。`tail` 只把共享文件输出到标准输出，不是具备远端确认能力的生产日志收集器；为避免伪造业务健康检查，示例没有随意添加始终成功的探针。生产中应换成实际收集器，并按照上一段定义探针和退出逻辑。[1]

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: sidecar-log-example
spec:
  template:
    spec:
      restartPolicy: Never
      terminationGracePeriodSeconds: 30
      initContainers:
        - name: log-sidecar
          image: alpine:3.22
          restartPolicy: Always
          command: [sh, -c, 'touch /logs/job.log; exec tail -n+1 -F /logs/job.log']
          volumeMounts:
            - name: logs
              mountPath: /logs
      containers:
        - name: work
          image: alpine:3.22
          command: [sh, -c, 'echo "job finished" >> /logs/job.log']
          volumeMounts:
            - name: logs
              mountPath: /logs
      volumes:
        - name: logs
          emptyDir: {}
```

迁移验收建议：检查最终 Pod 字段和节点兼容性；故意延迟 startupProbe，确认应用等待；让 readiness 失败，确认 Pod 不就绪但不误认成启动屏障；让 Sidecar 崩溃，确认其重启及应用容错；确认 Job 正常完成；分别测试正常退出、主应用耗尽宽限期和强制终止，核对日志端最终收到的记录；最后根据准入后的 requests/limits 检查配额和调度结果。只有这些故障场景有了证据，才能评估迁移后的业务可靠性。[1][2][4][5]

引用资料均在本次独立获取；原始 HTML、正文、检索结果、时间和 SHA-256 保存在 `.runs/marketscout-comparison-20261016/codex/`。Bing 两次检索返回非官方结果，未作为正文证据；随后使用 Tavily 定向搜索及官方交叉链接。2023 年 Alpha 博客仅用于版本背景检查，没有把其当年的已知问题套用到当前版本。

- [1] [Sidecar Containers](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/)
- [2] [Adopting Sidecar Containers](https://kubernetes.io/docs/tutorials/configuration/pod-sidecar-containers/)
- [3] [Init Containers](https://kubernetes.io/docs/concepts/workloads/pods/init-containers/)
- [4] [Start Sidecar First: How To Avoid Snags](https://kubernetes.io/blog/2025/06/03/start-sidecar-first/)
- [5] [Pod Lifecycle：Pod shutdown and sidecar containers](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-shutdown-and-sidecar-containers)
- [6] [Pod Overhead](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-overhead/)
