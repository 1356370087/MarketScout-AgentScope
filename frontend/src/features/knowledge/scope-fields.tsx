"use client";
import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { SurfaceDialog } from "@/components/ui/workspace";
import { materialScopeFromParams, type MaterialScope } from "@/lib/knowledge-scope";
import type { KnowledgeSearchRequest } from "@/lib/contracts/knowledge";
import { MaterialScopePicker, useKnowledgeBases } from "./material-scope-picker";

export function useScopeFields() {
  const params = useSearchParams();
  const [query, setQuery] = useState(params.get("query") ?? "");
  const [scope, setScope] = useState<MaterialScope>(() => materialScopeFromParams(params));
  const bases = useKnowledgeBases();
  const buildRequest = (): KnowledgeSearchRequest => ({ ...scope, query,
    kb_ids: scope.kb_ids.length || scope.collection_ids.length || scope.document_ids.length
      ? scope.kb_ids : (bases.data?.items ?? []).map((base) => base.id),
  });
  const fields = <><label><input value={query} onChange={(event) => setQuery(event.target.value)} aria-label="检索 / 提问内容" placeholder="检索 / 提问内容" /></label>
    <SurfaceDialog title="检索范围" description="选择可访问的知识库、集合及资料版本。" trigger={<button type="button">范围与筛选</button>}>
      <MaterialScopePicker value={scope} onChange={setScope} allowAll />
      <details><summary>指定文档范围</summary><input aria-label="文档范围" value={scope.document_ids.join(",")} onChange={(event) => setScope({ ...scope, document_ids: event.target.value.split(/[\s,]+/).filter(Boolean) })} placeholder="文档 ID，逗号分隔" /></details>
    </SurfaceDialog></>;
  return { query, fields, buildRequest, ready: query.trim().length > 0 && bases.isSuccess && (scope.version_mode !== "as_of" || !!scope.as_of_published) };
}
