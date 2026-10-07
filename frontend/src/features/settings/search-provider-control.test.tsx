import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { effectiveSearchProviders, sanitizeSettings, settingFieldBounds } from "@/lib/settings";
import { SearchProviderControl } from "./search-provider-control";

function Harness() {
  const [value, setValue] = useState<string[] | null>(null);
  return <><SearchProviderControl value={value} legacy="tavily" onChange={setValue} /><output data-testid="selection">{JSON.stringify(value)}</output></>;
}

describe("parallel search settings", () => {
  it("keeps the shadow timeout strictly above the backend's exclusive lower bound", () => {
    const rule = { type: "number", exclusiveMinimum: 0, maximum: 300 };
    expect(settingFieldBounds(rule, 60).minimum).toBeGreaterThan(0);
    const capabilities = { editable_config_keys: ["web_shadow_timeout_seconds"], config_schema: { properties: { web_shadow_timeout_seconds: rule } } };
    expect(sanitizeSettings({ web_shadow_timeout_seconds: 0 }, capabilities)).toEqual({});
    expect(sanitizeSettings({ web_shadow_timeout_seconds: 0.02 }, capabilities)).toEqual({ web_shadow_timeout_seconds: 0.02 });
  });
  it("inherits the old provider, selects peers and preserves an explicit empty selection", () => {
    render(<Harness />);
    expect(screen.getByRole("checkbox", { name: "Tavily" })).toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: "Bing" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Brave" }));
    expect(screen.getByTestId("selection")).toHaveTextContent('["tavily","bing","brave"]');
    for (const name of ["Tavily", "Bing", "Brave"]) fireEvent.click(screen.getByRole("checkbox", { name }));
    expect(screen.getByTestId("selection")).toHaveTextContent("[]");
    expect(screen.getByText(/已关闭搜索服务/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "恢复默认搜索渠道" }));
    expect(screen.getByTestId("selection")).toHaveTextContent("null");
    expect(screen.getByRole("checkbox", { name: "Tavily" })).toBeChecked();
  });

  it("saves arrays and null while rejecting unknown providers", () => {
    const capabilities = { editable_config_keys: ["search_providers"], config_schema: { properties: {
      search_providers: { anyOf: [{ type: "array" }, { type: "null" }] },
    } } };
    for (const value of [null, [], ["bing", "brave"]]) expect(sanitizeSettings({ search_providers: value }, capabilities)).toEqual({ search_providers: value });
    expect(sanitizeSettings({ search_providers: ["unknown"] }, capabilities)).toEqual({});
    expect(effectiveSearchProviders(null, "none")).toEqual([]);
    expect(effectiveSearchProviders([], "tavily")).toEqual([]);
  });
});
