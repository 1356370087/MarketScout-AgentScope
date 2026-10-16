"""运行已发布组件配置。命令由部署管理员在终端明确指定。"""

import argparse
import json
import os
from pathlib import Path
import runpy
import sys
import time

from .components import config_directory
from . import runtime


def main(argv=None):
    parser = argparse.ArgumentParser(description="使用数据库发布版本启动组件")
    parser.add_argument("--revision", help="已发布配置版本；省略时使用最新待应用版本")
    parser.add_argument("--component", choices=["sandbox-controller", "sandbox-gateway"], required=True)
    args = parser.parse_args(argv)
    module = "open_deep_research.sandbox." + ("controller" if args.component == "sandbox-controller" else "gateway")
    if not runtime.enabled():
        sys.argv = [args.component]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return
    directory = config_directory()
    if not args.revision:
        while not (directory / "desired.json").exists():
            time.sleep(3)
    revision = args.revision or json.loads((directory / "desired.json").read_text(encoding="utf-8"))["revision_id"]
    source = (directory / revision / "configuration.json").resolve()
    if directory not in source.parents:
        raise ValueError("configuration_revision_outside_store")
    value = json.loads(source.read_text(encoding="utf-8"))
    os.environ["PLATFORM_CONFIGURATION_MODE"] = "database"
    runtime.install(value)
    sys.argv = [args.component]
    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
