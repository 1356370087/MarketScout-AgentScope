"""配置发布和凭据维护使用普通事务、版本条件写入与同事务审计。"""

from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4
from sqlalchemy import select, update, insert, or_, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from . import schema as s
from .contracts import ConfigurationDocument
from .crypto import CredentialCipher


class PlatformError(ValueError):
    def __init__(self, detail: str, status: int = 422):
        super().__init__(detail)
        self.detail, self.status = detail, status


def now():
    return datetime.now(timezone.utc).isoformat()


def audit_now():
    """Return a timezone-aware value for the canonical time audit fields."""
    return datetime.now(timezone.utc)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require(principal, permission):
    if principal.user_id == "local-dev-user" or permission not in principal.permissions:
        raise PlatformError("insufficient_permissions", 403)


def check_scope(principal, scope, *, credentials=False):
    require(principal, ("platform.credentials.manage" if credentials else "platform.config.write")
            if scope == "global" else ("platform.credentials.personal" if credentials else "platform.config.personal"))


class PlatformStore:
    def __init__(self, engine, cipher=None):
        self.engine, self._cipher = engine, cipher

    @property
    def cipher(self):
        return self._cipher or CredentialCipher()

    def upsert(self, table):
        return (pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert)(table)

    async def audit(self, conn, actor, action, subject=None, detail=None):
        await conn.execute(insert(s.audit).values(id=str(uuid4()), actor_id=actor,
            action=action, subject_id=subject, detail=detail or {}, create_time=audit_now(),
            update_time=audit_now(), created_at=actor, updated_at=actor))

    async def state(self, conn=None):
        if conn is None:
            async with self.engine.connect() as conn:
                return await self.state(conn)
        row = (await conn.execute(select(s.state).where(s.state.c.id == 1))).mappings().one_or_none()
        if row is None:
            raise PlatformError("platform_schema_not_migrated", 503)
        return dict(row)

    async def active(self, conn=None):
        if conn is None:
            async with self.engine.connect() as conn:
                return await self.active(conn)
        state = await self.state(conn)
        if not state["active_revision"]:
            raise PlatformError("platform_configuration_required", 503)
        return await self.revision(state["active_revision"], conn=conn)

    async def revision(self, identity, principal=None, *, conn=None):
        if conn is None:
            async with self.engine.connect() as conn:
                return await self.revision(identity, principal, conn=conn)
        row = (await conn.execute(select(s.revisions, s.profiles.c.name, s.profiles.c.scope,
            s.profiles.c.owner_id).join(s.profiles, s.profiles.c.id == s.revisions.c.profile_id)
            .where(s.revisions.c.id == identity))).mappings().one_or_none()
        if row is None:
            raise PlatformError("configuration_not_found", 404)
        row = dict(row)
        if principal and row["scope"] == "personal" and row["owner_id"] != principal.user_id:
            raise PlatformError("configuration_not_found", 404)
        if principal and row["scope"] == "global" and row["status"] == "draft" and "platform.config.read" not in principal.permissions:
            raise PlatformError("configuration_not_found", 404)
        return row

    async def list_revisions(self, principal):
        async with self.engine.connect() as conn:
            query = select(s.revisions, s.profiles.c.name, s.profiles.c.scope, s.profiles.c.owner_id).join(
                s.profiles, s.profiles.c.id == s.revisions.c.profile_id).where(or_(
                s.profiles.c.owner_id == principal.user_id, s.profiles.c.scope == "global"))
            if "platform.config.read" not in principal.permissions:
                query = query.where(or_(s.profiles.c.owner_id == principal.user_id, s.revisions.c.status != "draft"))
            rows = (await conn.execute(query.order_by(s.revisions.c.create_time.desc()))).mappings().all()
            return [{k: v for k, v in row.items() if k != "document"} for row in rows]

    async def create_draft(self, principal, request, *, conn=None):
        check_scope(principal, request.scope)
        from .catalog import normalize_document
        request.document = normalize_document(request.document, scope=request.scope)
        if conn is None:
            async with self.engine.begin() as conn:
                return await self.create_draft(principal, request, conn=conn)
        document = request.document.model_dump(mode="json")
        await self.validate_credentials(conn, document, principal, request.scope)
        if request.scope == "personal":
            await self.validate_personal(conn, document, principal)
        digest = fingerprint(document)
        owner = principal.user_id if request.scope == "personal" else None
        duplicate = (await conn.execute(select(s.revisions.c.id).join(s.profiles,
            s.profiles.c.id == s.revisions.c.profile_id).where(s.profiles.c.owner_id == owner,
            s.revisions.c.status == "draft", s.revisions.c.fingerprint == digest))).scalar_one_or_none()
        if duplicate:
            return await self.revision(duplicate, principal, conn=conn)
        profile_id = request.profile_id or str(uuid4())
        if request.profile_id:
            profile = (await conn.execute(select(s.profiles).where(s.profiles.c.id == profile_id))).mappings().one_or_none()
            if not profile or profile["owner_id"] != owner or profile["scope"] != request.scope:
                raise PlatformError("configuration_not_found", 404)
        else:
            await conn.execute(insert(s.profiles).values(id=profile_id, name=request.name,
                owner_id=owner, scope=request.scope, create_time=audit_now(), update_time=audit_now(),
                created_at=principal.user_id, updated_at=principal.user_id))
        number = (await conn.scalar(select(func.max(s.revisions.c.number)).where(s.revisions.c.profile_id == profile_id)) or 0) + 1
        identity = str(uuid4())
        await conn.execute(insert(s.revisions).values(id=identity, profile_id=profile_id,
            number=number, version=1, status="draft", document=document, fingerprint=digest,
            base_revision=request.base_revision, created_by=principal.user_id,
            create_time=audit_now(), update_time=audit_now(),
            created_at=principal.user_id, updated_at=principal.user_id))
        await self.audit(conn, principal.user_id, "configuration.draft_created", identity)
        return await self.revision(identity, principal, conn=conn)

    async def update_draft(self, principal, identity, request):
        from .catalog import normalize_document
        async with self.engine.begin() as conn:
            row = await self.revision(identity, principal, conn=conn)
            check_scope(principal, row["scope"])
            request.document = normalize_document(request.document, scope=row["scope"])
            document = request.document.model_dump(mode="json")
            await self.validate_credentials(conn, document, principal, row["scope"])
            if row["scope"] == "personal":
                await self.validate_personal(conn, document, principal)
            changed = await conn.execute(update(s.revisions).where(s.revisions.c.id == identity,
                s.revisions.c.status == "draft", s.revisions.c.version == request.expected_version)
                .values(document=document, fingerprint=fingerprint(document), version=s.revisions.c.version + 1))
            if changed.rowcount != 1:
                raise PlatformError("configuration_version_conflict", 409)
            await self.audit(conn, principal.user_id, "configuration.draft_updated", identity)
            return await self.revision(identity, principal, conn=conn)

    async def validate_credentials(self, conn, document, principal, scope):
        from .importing import credential_references
        for reference in credential_references(document):
            identity, separator, version = reference.removeprefix("credential:").partition("@")
            row = await self.credential(identity, conn=conn)
            if row["disabled"] or (row["owner_id"] is not None and (scope == "global" or row["owner_id"] != principal.user_id)):
                raise PlatformError("credential_not_available", 403)
            if scope == "personal" and row["owner_id"] != principal.user_id:
                raise PlatformError("personal_model_requires_personal_credential", 403)
            if separator:
                if not version.isdecimal() or int(version) < 1:
                    raise PlatformError("credential_version_invalid")
                available = await conn.scalar(select(s.credential_versions.c.version).where(
                    s.credential_versions.c.credential_id == identity, s.credential_versions.c.version == int(version)))
                if available is None:
                    raise PlatformError("credential_version_unavailable")

    async def validate_personal(self, conn, document, principal):
        from .resolution import validate_overrides, validate_personal_models
        baseline = (await self.active(conn))["document"]
        validate_overrides(baseline, document["runtime"])
        validate_personal_models(baseline, document["litellm"])
        for key in ("services", "sandbox", "policy", "memory"):
            if document.get(key):
                raise PlatformError("个人配置只能调整平台开放的研究参数和模型接入", 403)
        if any(value is not None for value in document["quotas"].values()):
            raise PlatformError("个人配置不能修改平台配额", 403)

    async def publish(self, principal, identity, request):
        from .catalog import validate_document
        from .components import required_components
        async with self.engine.begin() as conn:
            row = await self.revision(identity, principal, conn=conn)
            if row["scope"] == "global":
                require(principal, "platform.config.publish")
            else:
                check_scope(principal, "personal")
            state = await self.state(conn)
            if state["active_revision"] != request.expected_platform_revision:
                raise PlatformError("platform_revision_conflict", 409)
            if row["scope"] == "global" and state["active_revision"]:
                old_policy = (await self.active(conn))["document"].get("sandbox")
                new_policy = row["document"].get("sandbox")
                if old_policy and new_policy and old_policy.get("deployment_id") != new_policy.get("deployment_id"):
                    raise PlatformError("现有部署的沙箱 deployment_id 必须保持不变")
            from .importing import resolve_references
            document = ConfigurationDocument.model_validate(resolve_references(row["document"], strict=True))
            validated = validate_document(document, publishing=row["scope"] == "global")
            await self.validate_credentials(conn, row["document"], principal, row["scope"])
            if row["scope"] == "personal":
                await self.validate_personal(conn, row["document"], principal)
            else:
                from .catalog import managed_runtime_keys, environment_catalog
                defaults = validated.model_dump(mode="json")
                document.runtime = {**{k: v for k, v in defaults.items() if k in managed_runtime_keys()}, **document.runtime}
                from open_deep_research.configuration import Configuration
                service_defaults = {key: item["default"] for key, item in environment_catalog().items()
                    if "default" in item and not item["bootstrap"] and not item["secret"] and key.lower() not in Configuration.model_fields}
                document.services = {**service_defaults, **document.services}
            published = document.model_dump(mode="json")
            published["credential_refs"] = {name: reference if reference.startswith("credential:") else "credential:" + reference
                for name, reference in published.get("credential_refs", {}).items()}
            from .importing import credential_ids
            credential_versions = {identity: (await self.credential(identity, conn=conn))["version"] for identity in credential_ids(published)}
            def pin(node):
                if isinstance(node, dict):
                    return {key: pin(value) for key, value in node.items()}
                if isinstance(node, list):
                    return [pin(value) for value in node]
                if isinstance(node, str) and node.startswith("credential:") and "@" not in node:
                    identity = node.removeprefix("credential:")
                    return node + "@" + str(credential_versions[identity])
                return node
            published = pin(published)
            inherited = (await self.active(conn))["document"] if row["scope"] == "personal" else published
            routing_policy = {key: inherited.get("litellm", {}).get(key, {}) for key in ("router_settings", "litellm_settings")}
            for model in published.get("litellm", {}).get("model_list", []):
                model["_platform_policy"] = routing_policy
            changed = await conn.execute(update(s.revisions).where(s.revisions.c.id == identity,
                s.revisions.c.status == "draft", s.revisions.c.version == request.expected_version)
                .values(status="pending", document=published, fingerprint=fingerprint(published), published_at=now(), base_revision=state["active_revision"], version=s.revisions.c.version + 1))
            if changed.rowcount != 1:
                raise PlatformError("configuration_version_conflict", 409)
            previous = await self.active(conn) if state["active_revision"] else None
            for component, mode in required_components(published, previous["document"] if previous else None).items():
                await conn.execute(insert(s.applications).values(revision_id=identity, component=component,
                    status="pending", detail={"mode": mode}, update_time=audit_now(),
                    updated_at=principal.user_id))
            await self.audit(conn, principal.user_id, "configuration.published", identity, {"reason": request.reason})
        return await self.revision(identity, principal)

    async def application(self, identity, component, status, detail):
        async with self.engine.begin() as conn:
            await conn.execute(update(s.applications).where(s.applications.c.revision_id == identity,
                s.applications.c.component == component).values(status=status, detail=detail,
                update_time=audit_now()))

    async def activate(self, principal, identity):
        async with self.engine.begin() as conn:
            row = await self.revision(identity, principal, conn=conn)
            if row["scope"] == "global":
                require(principal, "platform.config.publish")
            else:
                require(principal, "platform.config.personal")
            pending = await conn.scalar(select(func.count()).select_from(s.applications).where(
                s.applications.c.revision_id == identity, s.applications.c.status != "applied"))
            if pending:
                raise PlatformError("configuration_components_pending", 409)
            if row["status"] == "active":
                return row
            if row["status"] != "pending":
                raise PlatformError("configuration_not_published", 409)
            if row["scope"] == "global":
                condition = s.state.c.active_revision == row["base_revision"]
                changed = await conn.execute(update(s.state).where(s.state.c.id == 1, condition)
                    .values(active_revision=identity, generation=s.state.c.generation + 1))
                if changed.rowcount != 1:
                    raise PlatformError("platform_revision_conflict", 409)
            await conn.execute(update(s.revisions).where(s.revisions.c.id == identity).values(status="active"))
            await self.audit(conn, principal.user_id, "configuration.activated", identity)
        if row["scope"] == "global":
            from .components import config_directory, _atomic_json
            if (config_directory() / identity / "publisher.json").exists():
                _atomic_json(config_directory() / "active.json", {"revision_id": identity})
        return await self.revision(identity, principal)

    async def credential(self, identity, *, conn=None):
        if conn is None:
            async with self.engine.connect() as conn:
                return await self.credential(identity, conn=conn)
        row = (await conn.execute(select(s.credentials).where(s.credentials.c.id == identity))).mappings().one_or_none()
        if row is None:
            raise PlatformError("credential_not_found", 404)
        return dict(row)

    @staticmethod
    def public_credential(row):
        return {k: v for k, v in row.items() if k not in {"ciphertext", "key_id"}}

    async def create_credential(self, principal, request, *, conn=None):
        check_scope(principal, request.scope, credentials=True)
        if conn is None:
            async with self.engine.begin() as conn:
                return await self.create_credential(principal, request, conn=conn)
        if request.scope == "personal":
            allowed = (await self.active(conn))["document"]["allowed_providers"]
            if request.provider not in allowed:
                raise PlatformError("provider_not_allowed", 403)
        owner = principal.user_id if request.scope == "personal" else None
        candidates = (await conn.execute(select(s.credentials).where(s.credentials.c.owner_id == owner,
            s.credentials.c.name == request.name, s.credentials.c.provider == request.provider,
            s.credentials.c.disabled.is_(False)))).mappings().all()
        for candidate in candidates:
            decoded = self.cipher.decrypt(f"{candidate['id']}:{candidate['version']}:{owner or 'global'}", candidate["ciphertext"])
            if decoded == request.secret:
                return self.public_credential(candidate)
        identity = str(uuid4())
        cipher = self.cipher
        await conn.execute(insert(s.credentials).values(id=identity, name=request.name,
            owner_id=owner, provider=request.provider, version=1, disabled=False,
            ciphertext=cipher.encrypt(f"{identity}:1:{owner or 'global'}", request.secret),
            key_id=cipher.key_id, create_time=audit_now(), update_time=audit_now(),
            created_at=principal.user_id, updated_at=principal.user_id))
        await conn.execute(insert(s.credential_versions).values(credential_id=identity, version=1,
            ciphertext=(await self.credential(identity, conn=conn))["ciphertext"], key_id=cipher.key_id,
            create_time=audit_now(), update_time=audit_now(), created_at=principal.user_id,
            updated_at=principal.user_id))
        await self.audit(conn, principal.user_id, "credential.created", identity, {"provider": request.provider})
        return self.public_credential(await self.credential(identity, conn=conn))

    async def secret(self, identity, owner_id):
        identity, _, requested_version = identity.partition("@")
        row = await self.credential(identity)
        if row["disabled"] or (row["owner_id"] is not None and row["owner_id"] != owner_id):
            raise PlatformError("credential_not_available", 403)
        version = int(requested_version) if requested_version else row["version"]
        async with self.engine.connect() as conn:
            secret = (await conn.execute(select(s.credential_versions).where(
                s.credential_versions.c.credential_id == identity, s.credential_versions.c.version == version))).mappings().one()
        return self.cipher.decrypt(f"{identity}:{version}:{row['owner_id'] or 'global'}", secret["ciphertext"])

    async def preferences(self, owner):
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(s.preferences).where(s.preferences.c.owner_id == owner))).mappings().one_or_none()
            return dict(row) if row else {"profile_revision_id": None, "configurable": {}, "publication_theme": {}}

    async def limits(self, owner, conn=None):
        if conn is None:
            async with self.engine.connect() as conn:
                return await self.limits(owner, conn)
        baseline = (await self.active(conn))["document"]["quotas"]
        override = await conn.scalar(select(s.quotas.c.limits).where(s.quotas.c.owner_id == owner))
        return {**baseline, **{k: v for k, v in (override or {}).items() if v is not None}}


def get_store():
    from security.rbac.database import get_engine
    return PlatformStore(get_engine())
