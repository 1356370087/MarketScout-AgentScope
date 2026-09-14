import { describe, expect, it } from "vitest";
import { defaultPublicationTheme, modelChoicesFor, modelOptionsFor, sanitizePublicationTheme, sanitizeSettings, settingFieldBounds, settingFieldType, settingGroups } from "./settings";

describe("modelOptionsFor", () => {
  it("returns alias names for model fields when the litellm catalog is loaded", () => {
    const catalog = {
      backend: "litellm",
      models: [{ name: "if-research-v1" }, { name: "if-final-report-v1" }, { name: undefined }],
    };
    expect(modelOptionsFor("research_model", catalog)).toEqual([
      "if-research-v1",
      "if-final-report-v1",
    ]);
    expect(modelOptionsFor("final_report_model", catalog)).toEqual([
      "if-research-v1",
      "if-final-report-v1",
    ]);
  });

  it("falls back to null for non-model fields, legacy backend, or empty catalog", () => {
    const catalog = { backend: "litellm", models: [{ name: "if-research-v1" }] };
    expect(modelOptionsFor("search_api", catalog)).toBeNull();
    expect(modelOptionsFor("research_model", { backend: "legacy", models: [] })).toBeNull();
    expect(modelOptionsFor("research_model", { backend: "litellm", models: [] })).toBeNull();
    expect(modelOptionsFor("research_model", null)).toBeNull();
    expect(modelOptionsFor("research_model", undefined)).toBeNull();
  });

  it("exposes the report reviewer controls in the quality settings group", () => {
    const quality = settingGroups.find((group) => group.id === "quality");
    expect(quality?.keys).toEqual(expect.arrayContaining([
      "report_review_enabled",
      "report_review_model",
      "report_review_model_max_tokens",
      "report_review_temperature",
      "report_review_max_input_chars",
      "report_review_max_revisions",
      "report_review_fail_open",
    ]));
  });

  it("sanitizes nullable reviewer values and bounded revision controls", () => {
    const capabilities = {
      editable_config_keys: ["report_review_enabled", "report_review_model", "report_review_temperature", "report_review_max_revisions"],
      config_schema: { properties: {
        report_review_enabled: { type: "boolean" },
        report_review_model: { anyOf: [{ type: "string" }, { type: "null" }] },
        report_review_temperature: { anyOf: [{ type: "number", minimum: 0, maximum: 2 }, { type: "null" }] },
        report_review_max_revisions: { type: "integer", minimum: 0, maximum: 3 },
      } },
    };
    expect(sanitizeSettings({
      report_review_enabled: true, report_review_model: null, report_review_temperature: 0.1,
      report_review_max_revisions: 1, ignored: "drop",
    }, capabilities)).toEqual({
      report_review_enabled: true, report_review_model: null, report_review_temperature: 0.1,
      report_review_max_revisions: 1,
    });
    expect(sanitizeSettings({ report_review_temperature: "0.1", report_review_max_revisions: 4 }, capabilities)).toEqual({});
  });

  it("infers nullable numeric controls from anyOf and preserves their bounds", () => {
    const schema = { anyOf: [{ type: "number", minimum: 0, maximum: 2 }, { type: "null" }] };
    expect(settingFieldType(schema, null)).toBe("number");
    expect(settingFieldBounds(schema, null)).toEqual({ minimum: 0, maximum: 2 });
  });

  it("keeps an explicit unset choice for nullable model fields", () => {
    const schema = { anyOf: [{ type: "string" }, { type: "null" }] };
    expect(modelChoicesFor(schema, null, ["if-review-v1"])).toEqual(["", "if-review-v1"]);
    expect(modelChoicesFor(schema, "custom:model", ["if-review-v1"])).toEqual(["", "custom:model", "if-review-v1"]);
  });
});

describe("sanitizePublicationTheme", () => {
  it("normalizes bounded theme fields and rejects arbitrary values", () => {
    expect(sanitizePublicationTheme({
      ...defaultPublicationTheme,
      primary_color: "#0f766e",
      footer_text: "line\u0000 break",
      font_family: "remote-font",
      css: "body{}",
    })).toEqual({
      ...defaultPublicationTheme,
      primary_color: "#0F766E",
      footer_text: "line  break",
    });
  });
});
