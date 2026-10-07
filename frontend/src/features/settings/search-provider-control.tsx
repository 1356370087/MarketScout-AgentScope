"use client";

import { effectiveSearchProviders, WEB_SEARCH_PROVIDERS } from "@/lib/settings";

const labels = { tavily: "Tavily", openai: "OpenAI", anthropic: "Anthropic", bing: "Bing", brave: "Brave" };

export function SearchProviderControl({ value, legacy, onChange }: {
  value: unknown; legacy: unknown; onChange: (value: string[] | null) => void;
}) {
  const selected = effectiveSearchProviders(value, legacy);
  return <fieldset className="search-provider-control" aria-label="并行搜索服务">
    <legend>选择搜索渠道</legend>
    <div className="search-provider-options">{WEB_SEARCH_PROVIDERS.map((provider) => <label key={provider}>
      <input type="checkbox" checked={selected.includes(provider)} onChange={(event) => onChange(event.target.checked ? [...selected, provider] : selected.filter((item) => item !== provider))} />
      <span>{labels[provider]}</span>
    </label>)}</div>
    <small>{selected.length ? "所选渠道并行搜索，合并结果后统一核对来源。" : "已关闭搜索服务，仍可读取获准的指定网页。"}</small>
    <button type="button" className="text-button" onClick={() => onChange(null)}>恢复默认搜索渠道</button>
  </fieldset>;
}
