"""Cheap, reproducible checks for explicitly requested Markdown deliverables."""

import hashlib
import re

from .canonical import canonicalize_report
from .models import ReportReview, ReportReviewIssue


def format_review(markdown, messages, attempt):
    requests = []
    for message in messages:
        role = getattr(message, "type", None)
        if role in {"human", "user"}:
            requests.append(str(message.content))
        elif isinstance(message, dict) and message.get("role", message.get("type")) in {"human", "user"}:
            requests.append(str(message.get("content", "")))
    request = "\n".join(requests)
    limit = re.search(r"(?:不超过|最多|上限(?:为)?|至多|≤|at most|max(?:imum)?\.?)\s*(\d[\d,，]*)\s*(?:个?中?文?字|characters?)", request, re.I)
    if not limit:
        limit = re.search(r"(?:\d[\d,，]*\s*[-–—～至到]\s*)?(\d[\d,，]*)\s*字(?:以内|以下)", request)
    table = any(re.search(r"(?:输出|包含|写|列|给出|提供|制作|附|使用|用|呈现).{0,12}(?:对比表|比较表|对照表)|用表格.{0,8}(?:对比|比较)|(?:include|provide|make|use) .{0,12}comparison table", part, re.I)
                and not re.search(r"不(?:要|用|需要)|无需|不必|do not|without", part, re.I)
                for part in re.split(r"[。！？；;\n，]", request))
    if not (table or limit):
        return None
    report = canonicalize_report(markdown, run_id="format-check", fallback_title="")
    parts = [section.title for section in report.sections]
    if re.search(r"^#\s", markdown, re.M):
        parts.append(report.title)
    valid_table = False
    for block in [*report.summary_blocks, *(b for section in report.sections for b in section.blocks)]:
        if block.type == "table":
            header = ["".join(run.text for run in cell).strip() for cell in block.headers]
            rows = [["".join(run.text for run in cell).strip() for cell in row] for row in block.rows]
            valid_table |= sum(bool(cell) for cell in header) >= 2 and any(
                sum(bool(cell) for cell in row) >= 2 for row in rows)
            parts.extend(header)
            parts.extend(cell for row in rows for cell in row)
        elif block.type == "list":
            parts.extend(run.text for item in block.items for run in item)
        elif block.type == "code":
            parts.append(block.text)
        else:
            parts.extend(run.text for run in block.runs)
    count = len(re.sub(r"https?://\S+|\s+", "", "\n".join(parts)))
    issues = []
    if limit:
        maximum = int(re.sub("[,，]", "", limit[1]))
        if count > maximum:
            issues.append(f"正文有 {count} 个可见字符，用户要求最多 {maximum} 个。")
    if table and not valid_table:
        issues.append("用户要求对比表，正文缺少含表头和数据行的可渲染表格。")
    if not issues:
        return None
    return ReportReview(decision="revise", status="completed", attempt=attempt, model="format_preflight",
        summary=" ".join(issues), deterministic_failures=["requested_format"], draft_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
        issues=[ReportReviewIssue(category="executive_readability", severity="high", blocking=True,
            location="正文", description=issue, revision_instruction=issue) for issue in issues])
