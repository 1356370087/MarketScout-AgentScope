import { Radar, ShieldCheck } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { ThemeToggle } from "@/components/theme-toggle";

export function AuthShell({ eyebrow, title, copy, children, footer }: {
  eyebrow: string; title: string; copy: string; children: ReactNode; footer?: ReactNode;
}) {
  return <main className="login-page auth-page">
    <section className="login-art">
      <div className="brand-lockup"><div className="brand-mark"><Radar /></div><div><strong>InsightForge</strong><span>企业竞争情报研究台</span></div></div>
      <div><span className="eyebrow">COMPETITIVE INTELLIGENCE / EVIDENCE FIRST</span><h1>看见竞争，<br />早于共识。</h1><p>把分散的市场信号，锻造成可验证、可复核、可行动的判断。</p></div>
      <span className="eyebrow"><ShieldCheck size={13} /> SELF-HOSTED IAM · ARGON2ID · EDDSA</span>
    </section>
    <section className="auth-stage">
      <div className="auth-theme"><ThemeToggle compact /></div>
      <div className="auth-card panel">
        <span className="eyebrow">{eyebrow}</span>
        <h2>{title}</h2>
        <p className="auth-copy">{copy}</p>
        {children}
        {footer && <div className="auth-footer">{footer}</div>}
      </div>
      <Link className="auth-home" href="/">返回 InsightForge</Link>
    </section>
  </main>;
}
