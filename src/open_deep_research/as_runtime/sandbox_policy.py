"""沙箱权限与网络网关的原生桥接（T032）。

把既有 V7 领域服务（HKDF 派生能力令牌、三层出网模式合成、版本化目标审
批、auto 模式两阶段分类器）接入 AgentScope 运行时，不引入第二套权限体
系：

- ``CapabilityTokenIssuer``：能力令牌签发（TaskTokenClaimsV1 + HMAC），
  绑定 run/task/fence/profile/policy 摘要；
- ``EgressModeBridge``：TOML 基线 × run 级设置 × 运行时覆盖，取最窄；
  越过基线的放宽请求 fail-closed 回落；
- ``EgressAuthority``：目标判定统一入口——人工决策（版本化）> 冻结策略
  （allow/deny 域与端口）> 模式（manual=ask / open=allow / auto=分类
  器）；
- ``NativeClassifierInvoker``：分类器模型调用走原生 ``egress_classifier``
  角色；
- 密钥注入范围：Worker 侧 runtime_config 在 wire 层拒绝一切凭据形状键，
  容器唯一令牌是任务令牌（``assert_worker_secret_scope``）。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from open_deep_research.sandbox.approvals import SecurityApprovalStore
from open_deep_research.sandbox.crypto import (
    SandboxDerivedKeys,
    decode_task_token,
    encode_task_token,
)
from open_deep_research.sandbox.egress_classifier import EgressClassifier
from open_deep_research.sandbox.egress_ledger_store import RunEgressModeStore
from open_deep_research.sandbox.egress_mode import effective_egress_mode
from open_deep_research.sandbox.schema import network_target_decision
from open_deep_research.sandbox.wire import SandboxTaskPayloadV1, TaskTokenClaimsV1

Decision = Literal["allow", "deny", "ask"]


class CapabilityTokenIssuer:
    """能力令牌签发器：根密钥 HKDF 派生，claims 绑定任务五元组。"""

    def __init__(self, keys: SandboxDerivedKeys) -> None:
        self.keys = keys

    @classmethod
    def from_root_key(cls, root_key: str) -> CapabilityTokenIssuer:
        return cls(SandboxDerivedKeys.from_root(root_key))

    @classmethod
    def for_controller(cls, controller: Any) -> CapabilityTokenIssuer:
        """从控制器客户端复用同一组派生密钥（同一信任域）。"""
        return cls(controller.keys)

    def issue(
        self,
        *,
        run_id: str,
        task_id: str,
        fence_token: int,
        profile_id: str,
        policy_digest: str,
        ttl_seconds: float,
    ) -> tuple[str, TaskTokenClaimsV1]:
        now = time.time()
        claims = TaskTokenClaimsV1(
            run_id=run_id,
            task_id=task_id,
            fence_token=fence_token,
            profile_id=profile_id,
            policy_digest=policy_digest,
            issued_at=now,
            expires_at=now + ttl_seconds,
            jti=uuid.uuid4().hex,
        )
        return encode_task_token(claims, self.keys.task_token), claims

    def verify(self, token: str) -> TaskTokenClaimsV1:
        """验签并检查过期；失败抛出底层校验错误。"""
        return decode_task_token(token, self.keys.task_token)


class EgressModeBridge:
    """三层出网模式合成：基线（冻结）× run 级（冻结）× 运行时覆盖。"""

    def __init__(
        self,
        *,
        baseline: str,
        run_setting: str = "profile",
        mode_store: RunEgressModeStore | None = None,
    ) -> None:
        self.baseline = baseline
        self.run_setting = run_setting
        self.mode_store = mode_store

    def effective(self, *, fence_token: int | None = None) -> Any:
        """合成当前有效模式；运行时覆盖按 fence 隔离（不匹配视为缺席）。"""
        runtime_override = None
        if self.mode_store is not None:
            override = self.mode_store.get()
            if override is not None and (
                fence_token is None or override.fence_token == fence_token
            ):
                runtime_override = override.mode
        return effective_egress_mode(
            self.baseline,
            run_setting=self.run_setting,
            runtime_override=runtime_override,
        )


@dataclass(frozen=True, slots=True)
class EgressTargetDecision:
    """统一目标判定结果。"""

    decision: Decision
    reason: str
    source: str
    target_id: str | None = None
    version: int | None = None


class NativeClassifierInvoker:
    """EgressModelInvoker 的原生实现：走 ``egress_classifier`` 角色策略。"""

    def __init__(self, factory: Any) -> None:
        self.factory = factory

    @staticmethod
    def _messages(call: Any) -> list:
        from agentscope.message import Msg

        return [
            Msg(
                "system" if item.get("role") == "system" else "user",
                str(item.get("content", "")),
                item.get("role", "user"),
            )
            for item in call.messages
        ]

    async def __call__(self, call: Any) -> Any:
        """按 EgressModelCall 契约执行一次调用并归一化回复。"""
        from open_deep_research.sandbox.egress_classifier import EgressModelReply

        messages = self._messages(call)
        middleware = self.factory.policy_middleware("egress_classifier")
        if call.structured_schema:
            async def structured_handler(current_model: Any, messages_, **_: Any):
                return await current_model.generate_structured_output(
                    messages_, call.structured_schema
                )

            result = await middleware.policy.invoke(
                structured_handler, {"messages": messages}, {}
            )
            return EgressModelReply(
                status="completed",
                structured=dict(result.content),
                served_model=self.factory.descriptor("egress_classifier")["model"],
            )

        async def text_handler(current_model: Any, messages_, **kwargs: Any):
            return await current_model(messages_, max_tokens=call.max_output_tokens)

        result = await middleware.policy.invoke(
            text_handler, {"messages": messages}, {}
        )
        return EgressModelReply(
            status="completed",
            content=result.get_text_content(),
            served_model=self.factory.descriptor("egress_classifier")["model"],
        )


class EgressAuthority:
    """出网目标判定权威：人工决策 > 冻结策略 > 模式。"""

    def __init__(
        self,
        *,
        policy: Any,
        mode_bridge: EgressModeBridge,
        approvals: SecurityApprovalStore,
        classifier: EgressClassifier | None = None,
    ) -> None:
        self.policy = policy
        self.mode_bridge = mode_bridge
        self.approvals = approvals
        self.classifier = classifier

    async def decide(
        self,
        run_id: str,
        *,
        host: str,
        port: int,
        capability: str = "tool.egress",
        tool_name: str = "",
        intent: str = "",
        fence_token: int | None = None,
    ) -> EgressTargetDecision:
        # Tier 1：版本化人工目标决策（allow_run / block_run / revoke）。
        if fence_token is not None:
            state = self.approvals.check_target(
                capability, {"host": host, "port": port}, fence_token
            )
            decision = state.get("decision")
            if decision == "allow_run":
                return EgressTargetDecision(
                    "allow", "human_allow_run", "human",
                    target_id=state.get("target_id"), version=state.get("version"),
                )
            if decision == "block_run":
                return EgressTargetDecision(
                    "deny", "human_block_run", "human",
                    target_id=state.get("target_id"), version=state.get("version"),
                )
            # revoke / 未决策 → 继续走常规判定
        # Tier 2：冻结策略（allow/deny 域与端口、unknown_target）。
        policy_decision = network_target_decision(self.policy, host, port)
        if policy_decision == "allow":
            return EgressTargetDecision("allow", "policy_allowlist", "policy")
        if policy_decision == "deny":
            return EgressTargetDecision("deny", "policy_deny", "policy")
        # Tier 3：模式桥接（ask → 人工；auto → 分类器；ask 保持待审批）。
        effective = self.mode_bridge.effective(fence_token=fence_token)
        mode = effective.mode
        if mode == "open":
            return EgressTargetDecision("allow", "mode_open", "mode")
        if mode != "auto" or self.classifier is None or capability != "tool.egress":
            return EgressTargetDecision("ask", f"mode_{mode}", "mode")
        classification = await self.classifier.classify_target(
            host=host,
            port=port,
            tool_name=tool_name,
            capability=capability,
            intent=intent,
            invoker=self._classifier_invoker(),
            allow_domains=self.policy.allow_domains,
            allow_ports=self.policy.allow_ports,
            allow_http_methods=self.policy.allow_http_methods,
        )
        verdict = classification.verdict
        if verdict == "allow":
            return EgressTargetDecision("allow", "classifier_allow", "classifier")
        if verdict == "deny":
            return EgressTargetDecision("deny", "classifier_deny", "classifier")
        return EgressTargetDecision("ask", "classifier_ask", "classifier")

    def _classifier_invoker(self) -> Any:
        invoker = getattr(self, "_invoker", None)
        if invoker is None:
            raise RuntimeError("auto mode requires a classifier model invoker")
        return invoker

    def bind_classifier_invoker(self, invoker: Any) -> None:
        self._invoker = invoker

    def decide_target_versioned(
        self,
        target_id: str,
        *,
        decision: str,
        reason: str,
        actor: str,
        expected_version: int,
        fence_token: int,
    ) -> dict[str, Any]:
        """版本化目标决策（乐观并发）；冲突抛 ValueError。"""
        return self.approvals.decide_target(
            target_id,
            decision=decision,
            reason=reason,
            actor=actor,
            expected_version=expected_version,
            fence_token=fence_token,
        )


def assert_worker_secret_scope(payload: SandboxTaskPayloadV1) -> None:
    """Worker 密钥边界：runtime_config 不得携带任何凭据形状键。

    wire 层的校验器在此处显式复核一次——签发/装配路径据此 fail-closed，
  密钥注入范围不因原生运行时扩大（容器唯一秘密是任务令牌）。
    """
    forbidden = [
        key
        for key in payload.runtime_config
        if key.lower() in {"apikeys", "api_key", "password", "credential"}
        or any(
            suffix in key.lower()
            for suffix in ("_api_key", "_secret_key", "_auth_token", "_password", "_credential")
        )
    ]
    if forbidden:
        raise ValueError(
            "sandbox runtime_config contains credential-shaped keys: "
            + ",".join(sorted(forbidden))
        )


__all__ = [
    "CapabilityTokenIssuer",
    "EgressAuthority",
    "EgressModeBridge",
    "EgressTargetDecision",
    "NativeClassifierInvoker",
    "assert_worker_secret_scope",
]
