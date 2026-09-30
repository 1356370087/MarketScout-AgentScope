import { Radar } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { ThemeToggle } from "@/components/theme-toggle";

export function AuthShell({ eyebrow, title, copy, children, footer }: {
  eyebrow: string; title: string; copy: string; children: ReactNode; footer?: ReactNode;
}) {
  return <main className="auth-workspace"><header className="auth-workspace-header"><Link className="brand-lockup" href="/"><div className="brand-mark"><Radar size={20} /></div><strong>InsightForge</strong></Link><ThemeToggle compact /></header><section className="auth-workspace-card"><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p className="auth-copy">{copy}</p>{children}{footer && <div className="auth-footer">{footer}</div>}</section><footer className="auth-workspace-footer">让每一个判断，都有据可循。<Link href="/">返回首页</Link></footer></main>;
}
