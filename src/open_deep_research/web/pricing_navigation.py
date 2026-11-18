"""Navigation suggestions for explicitly missing pricing fields on official sites."""

import re
from urllib.parse import urljoin, urlsplit


def requested_field(objective):
    if not re.search(r"pric|billing|套餐|报价|价格|计费|定价|月付|年付|币种", objective, re.I):
        return None
    if re.search(r"currency|币种|货币", objective, re.I):
        return "currency"
    if re.search(r"annual|year|年付|年价|年缴", objective, re.I):
        return "annual"
    if re.search(r"unit|单位|工作区", objective, re.I):
        return "unit"
    return "price"


def has_field(excerpts, field):
    patterns = {
        "currency": r"\b(?:USD|EUR|CNY|GBP|JPY|CAD|AUD|RMB)\b|美元|人民币",
        "annual": r"(?:\d[\d,. ]*[^\n]{0,15}(?:year|annual|/\s*年|元/年)|年付[^\n]{0,20}\d)",
        "unit": r"(?:per\s+(?:workspace|team|seat|user)|每(?:个)?(?:工作区|团队|席位|用户))",
        "price": r"(?:[$€£¥￥]|\b(?:USD|EUR|CNY|GBP)\s*)\d|\d[\d,.]*\s*元",
    }
    return any(re.search(patterns[field], text, re.I) for text in excerpts)


def published_links(document):
    matches = list(document.alternate_urls)
    for label, href in re.findall(r"\[([^\]\n]*)\]\(([^)\s]+)\)", document.markdown):
        try:
            target = urljoin(document.final_url, href.strip("<>"))
            path = urlsplit(target).path
        except ValueError:
            continue
        if re.search(r"pric|billing|tariff|套餐|定价|llms\.txt", label + " " + path, re.I):
            matches.append(target)
    return list(dict.fromkeys(matches))[:32]


def discovery_links(document, config, *, allow_index):
    return authorized_links(document.final_url, published_links(document), config, allow_index=allow_index)


def authorized_links(website, matches, config, *, allow_index):
    from open_deep_research.agentscope_runtime.search_providers import source_allowed
    from open_deep_research.documents.contracts import selection_from_config
    links = [url for url in matches if source_allowed(url, config)]
    if not links and allow_index and selection_from_config(config).domains:
        index = urljoin(website, "/llms.txt")
        # Domain grants and exact URL grants can coexist. The domain itself
        # must authorize probing this well-known directory.
        host = urlsplit(index).hostname or ""
        if any(host == domain or host.endswith("." + domain) for domain in selection_from_config(config).domains) and source_allowed(index, config):
            links.append(index)
    return links[:16]


def pricing_attempts(result, config, prior, records):
    """Remember one field and method per document version, across both Web tools."""
    from open_deep_research.agentscope_runtime.efficiency import fingerprint
    from open_deep_research.security.content import inspect_untrusted_content
    field = requested_field(result.request.objective)
    if not field:
        return {}, []
    pages = [(d.requested_url, d.final_url, d.content_hash, published_links(d), False)
             for d in result.documents if not inspect_untrusted_content(d.markdown)]
    acquired = {url for url, *_ in pages}
    for fetch in result.fetches:
        if fetch.requested_url in acquired:
            continue
        thin = next((a["pricing_navigation"] for a in fetch.backend_attempts if "pricing_navigation" in a), None)
        if thin:
            pages.append((fetch.requested_url, thin["url"], thin["content_hash"], thin["links"], True))
    attempts, suggestions = {}, []
    for requested, final, version, links, thin in pages:
        found = has_field((r["supporting_excerpt"] for r in records if r["source_url"] in {requested, final}), field)
        index = "index:" + (urlsplit(final).hostname or "")
        navigation = [] if found else authorized_links(final, links, config, allow_index=index not in prior)
        attempts[fingerprint([requested, version, field, "evidence"])] = {"url": requested, "content_hash": version,
            "field": field, "method": "evidence", "found": found, "navigation": navigation, "navigation_only": thin}
        suggestions.extend(navigation)
    return attempts, list(dict.fromkeys(suggestions))
