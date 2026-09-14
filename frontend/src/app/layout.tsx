import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/noto-sans-sc/400.css";
import "./globals.css";
import "./insight-theme.css";
import type { Metadata } from "next";
import { cookies } from "next/headers";
import { Providers } from "./providers";
import type { Locale } from "@/i18n/messages";

export const metadata: Metadata = {
  title: { default: "InsightForge · 企业竞品分析", template: "%s · InsightForge" },
  description: "面向企业竞争情报的可验证深度研究工作台",
};

const themeInitScript = `
try {
  const theme = localStorage.getItem("odr.interface-theme.v1") === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
} catch (_) {
  document.documentElement.dataset.theme = "light";
  document.documentElement.style.colorScheme = "light";
}`;

export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const cookieStore = await cookies();
  const raw = cookieStore.get("odr.locale")?.value;
  const locale: Locale = raw === "en" ? "en" : "zh-CN";
  return <html lang={locale} data-theme="light" suppressHydrationWarning>
    <head><script dangerouslySetInnerHTML={{ __html: themeInitScript }} /></head>
    <body><Providers locale={locale}>{children}</Providers></body>
  </html>;
}
