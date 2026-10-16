"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useSyncExternalStore } from "react";
import { ArrowLeftRight } from "lucide-react";
import { canOpenAdmin, workspaceReturnPath } from "@/lib/platform";
import type { AuthUser } from "@/lib/auth";
import "./workspace-switch.css";

const subscribe = () => () => {};

export function WorkspaceSwitch({ identity, admin = false }: { identity?: AuthUser; admin?: boolean }) {
  const pathname = usePathname();
  const returnPath = useSyncExternalStore(subscribe, () => workspaceReturnPath(identity ? sessionStorage.getItem(`odr.workspace-return.${identity.id}`) : null), () => "/research/new");
  useEffect(() => {
    if (!identity) return;
    const key = `odr.workspace-return.${identity.id}`;
    if (!admin) sessionStorage.setItem(key, workspaceReturnPath(pathname + window.location.search));
  }, [identity, admin, pathname]);
  if (!admin && !canOpenAdmin(identity?.permissions)) return null;
  return <Link className="workspace-switch" href={admin ? returnPath : "/admin"}><ArrowLeftRight size={15} /><span>{admin ? "切换到研究工作台" : "切换到管理后台"}</span></Link>;
}
