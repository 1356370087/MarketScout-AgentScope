"""Authorized domain ports for native knowledge tools and resource sharing."""

from importlib import import_module
from typing import Any

from agentscope.app.access import (
    ResourceAccessPolicyBase,
    ResourceKind,
    ResourcePermission,
    ResourceRef,
)
from pydantic import BaseModel, ConfigDict, Field

from open_deep_research.knowledge import authz
from open_deep_research.knowledge.search_service import SearchRequest, unified_search
from open_deep_research.tools.base import (
    ToolEffect,
    ToolExecutionZone,
    ToolOrigin,
    ToolResult,
    build_tool,
)

# Domain functions retain publication transactions, revision CAS, authorization,
# audit and durable job ownership. Never expose arbitrary module/function lookup.
OPERATIONS = {
    "versions": ("documents.versioning", "list_versions", False),
    "generations": ("documents.versioning", "list_generations", False),
    "generation_detail": ("documents.versioning", "generation_review_detail", False),
    "publish": ("documents.versioning", "publish_generation", True),
    "reject": ("documents.versioning", "reject_generation", True),
    "withdraw": ("documents.versioning", "withdraw_version", True),
    "correct": ("documents.corrections", "apply_corrections", True),
    "reparse": ("documents.reparse", "queue_scoped_reparse", True),
    "batch_create": ("knowledge.batches", "create_batch", True),
    "batch_get": ("knowledge.batches", "get_batch", False),
    "batch_cancel": ("knowledge.batches", "cancel_batch", True),
    "batch_retry": ("knowledge.batches", "retry_failed", True),
    "batch_execute": ("knowledge.batches", "execute_batch", True),
    "trash": ("knowledge.trash", "trash_document", True),
    "restore": ("knowledge.trash", "restore_document", True),
    "trash_list": ("knowledge.trash", "list_trash", False),
    "sync_create": ("knowledge.sync", "create_sync_source", True),
    "sync_run": ("knowledge.sync", "run_sync", True),
    "fact_list": ("knowledge.facts", "list_assertions", False),
    "fact_submit": ("knowledge.facts", "submit_assertion", True),
    "fact_publish": ("knowledge.facts", "publish_assertion", True),
    "fact_reject": ("knowledge.facts", "reject_assertion", True),
    "fact_withdraw": ("knowledge.facts", "withdraw_assertion", True),
    "wiki_create": ("knowledge.wiki", "create_page", True),
    "wiki_save": ("knowledge.wiki", "save_draft", True),
    "wiki_generate": ("knowledge.wiki", "generate_page", True),
    "wiki_publish": ("knowledge.wiki", "publish_page", True),
    "wiki_get": ("knowledge.wiki", "get_page", False),
    "health": ("knowledge.health", "dashboard", False),
    "export": ("knowledge.exporter", "export_knowledge_base", True),
    "import": ("knowledge.exporter", "import_archive", True),
}


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    kb_ids: list[str] = Field(default_factory=list)
    collection_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    generation_ids: list[str] = Field(default_factory=list)
    version_mode: str = "current"
    as_of_published: str | None = None
    as_of_valid: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=12, ge=1, le=100)


class KnowledgeApplication:
    """Bind identity from IAM and recheck coarse permission on every operation."""

    def __init__(self, actor_id, authorize, *, authorize_url=None):
        self.actor_id, self.authorize = actor_id, authorize
        self.authorize_url = authorize_url

    async def search(self, request: SearchInput, *, answer=False):
        await self.authorize("answer" if answer else "search")
        scoped = SearchRequest(owner_id=self.actor_id, **request.model_dump())
        if answer:
            from open_deep_research.knowledge.answer import answer_question

            return await answer_question(scoped)
        return await unified_search(scoped)

    async def execute(self, command, **arguments):
        module, symbol, _ = OPERATIONS[command]
        await self.authorize(command)
        if command == "sync_run" and self.authorize_url is None:
            raise PermissionError("sync requires host egress authorization")
        if command == "sync_create":
            await self._document_capability(
                arguments["document_id"],
                authz.CAP_MANAGE,
                arguments["knowledge_base_id"],
            )
        if command == "sync_run":
            from open_deep_research.documents.database import get_document_pool

            pool = await get_document_pool()
            async with pool.acquire() as connection:
                kb = await connection.fetchval(
                    "SELECT knowledge_base_id FROM knowledge_sync_sources WHERE id=$1::uuid",
                    arguments["source_id"],
                )
            if not kb:
                raise PermissionError("sync source not found or forbidden")
            await authz.require_kb_capability(self.actor_id, str(kb), authz.CAP_MANAGE)
        if command == "batch_create":
            capability = (
                authz.CAP_MANAGE
                if arguments["operation"] == "trash"
                else authz.CAP_SUBMIT
            )
            for document_id in arguments["document_ids"]:
                await self._document_capability(
                    document_id, capability, arguments.get("knowledge_base_id")
                )
        if command in {"batch_execute", "batch_retry"}:
            from open_deep_research.knowledge.batches import get_batch

            batch = await get_batch(self.actor_id, arguments["batch_id"])
            if not batch:
                raise PermissionError("batch not found or forbidden")
            capability = (
                authz.CAP_MANAGE if batch["operation"] == "trash" else authz.CAP_SUBMIT
            )
            for item in batch["items"]:
                if item["status"] in {"pending", "failed"}:
                    await self._document_capability(item["document_id"], capability)
        if command in {"trash", "restore"}:
            from open_deep_research.documents.database import get_document_pool

            pool = await get_document_pool()
            async with pool.acquire() as connection:
                kb = await connection.fetchval(
                    "SELECT home_knowledge_base_id FROM research_documents WHERE id=$1::uuid",
                    arguments["document_id"],
                )
            if not kb:
                raise PermissionError("document not found or forbidden")
            await authz.require_kb_capability(self.actor_id, str(kb), authz.CAP_MANAGE)
        if command == "trash_list":
            await authz.require_kb_capability(
                self.actor_id, arguments["knowledge_base_id"], authz.CAP_MANAGE
            )
        if command == "sync_run":
            if self.authorize_url is None:
                raise PermissionError("sync requires host egress authorization")
            arguments["authorize_url"] = self.authorize_url
        # The first domain argument is always the actor. Passing it positionally
        # makes an attempted actor/owner override a TypeError before any SQL.
        function = getattr(import_module("open_deep_research." + module), symbol)
        return await function(self.actor_id, **arguments)

    async def _document_capability(self, document_id, capability, expected_kb=None):
        from open_deep_research.documents.database import get_document_pool

        pool = await get_document_pool()
        async with pool.acquire() as connection:
            kb = await connection.fetchval(
                "SELECT home_knowledge_base_id FROM research_documents WHERE id=$1::uuid",
                document_id,
            )
        if not kb or (expected_kb and str(kb) != expected_kb):
            raise PermissionError("document outside knowledge scope")
        await authz.require_kb_capability(self.actor_id, str(kb), capability)

    def operation_tools(self, allowed_operations):
        """Expose only the host-selected catalog, with domain argument schemas."""
        import inspect
        from typing import get_type_hints

        from pydantic import create_model

        tools = []
        for command in allowed_operations:
            module, symbol, writes = OPERATIONS[command]
            function = getattr(import_module("open_deep_research." + module), symbol)
            hints = get_type_hints(function)
            parameters = list(inspect.signature(function).parameters.values())[1:]
            fields = {
                p.name: (
                    hints.get(p.name, Any),
                    ... if p.default is inspect.Parameter.empty else p.default,
                )
                for p in parameters
                if p.name != "authorize_url"
            }
            schema = create_model(
                "Knowledge_" + command, __config__=ConfigDict(extra="forbid"), **fields
            )

            async def call(input, context, progress, name=command):
                return ToolResult(output=await self.execute(name, **input.model_dump()))

            tools.append(
                build_tool(
                    name="knowledge_" + command,
                    input_schema=schema,
                    description=function.__doc__ or command,
                    call=call,
                    origin=ToolOrigin.SYSTEM,
                    effect=ToolEffect.LOCAL_WRITE if writes else ToolEffect.READ_ONLY,
                    execution_zone=ToolExecutionZone.HOST_CONTROL,
                    concurrency_safe=not writes,
                    retryable=False,
                )
            )
        return tools

    def search_tool(self):
        async def search(input, context, progress):
            return ToolResult(output=await self.search(input))

        return build_tool(
            name="knowledge_search",
            input_schema=SearchInput,
            description="检索已授权知识库，保留发布代次、来源和历史时间过滤。",
            call=search,
            origin=ToolOrigin.SYSTEM,
            effect=ToolEffect.READ_ONLY,
            execution_zone=ToolExecutionZone.HOST_CONTROL,
            concurrency_safe=True,
        )


class KnowledgeAccessPolicy(ResourceAccessPolicyBase):
    """Map explicit native resource bindings; domain membership stays authoritative.

    bindings is a host-owned mapping native id -> (domain kb id, native owner).
    It does not create native collections or mirror domain content.
    """

    def __init__(self, bindings):
        self.bindings = dict(bindings)

    async def list_accessible(self, viewer_id, kind, storage):
        if kind != ResourceKind.KNOWLEDGE_BASE:
            return []
        refs = []
        for native_id, (kb_id, owner_id) in self.bindings.items():
            if viewer_id != owner_id and authz.CAP_VIEW in await authz.kb_capabilities(
                viewer_id, kb_id
            ):
                refs.append(
                    ResourceRef(
                        kind=kind,
                        owner_id=owner_id,
                        resource_id=native_id,
                        permission=ResourcePermission.READ,
                    )
                )
        return refs

    async def can_edit(self, viewer_id, kind, owner_id, resource_id, storage):
        # Native generic mutation bypasses domain review/revision transactions.
        # All mutations must use KnowledgeApplication and its domain services.
        return False
