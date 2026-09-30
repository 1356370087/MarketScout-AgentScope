"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export function KnowledgeNav() {
  const pathname = usePathname();
  return <nav className="knowledge-nav" aria-label="知识空间导航">{[["/knowledge", "检索与问答"], ["/knowledge/ledger", "事实与 Wiki"], ["/knowledge/health", "健康与缺口"]].map(([href, label]) => <Link key={href} href={href} aria-current={pathname === href ? "page" : undefined}>{label}</Link>)}</nav>;
}
