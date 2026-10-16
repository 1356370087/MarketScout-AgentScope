"""从数据库发布的加密产物启动 LiteLLM；兼容代理镜像中的 Python 3.12。"""

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from urllib.request import urlopen

import yaml
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def main():
    if os.getenv("PLATFORM_CONFIGURATION_MODE") == "explicit":
        os.execvp("litellm", ["litellm", "--config", os.getenv("LITELLM_CONFIG_PATH", "/app/config/litellm.yaml"), "--host", "0.0.0.0", "--port", "4000"])
    root = Path(os.getenv("PLATFORM_CONFIG_DIR", "/data/platform-config"))
    desired = root / "desired.json"
    bundle = None
    if desired.exists():
        revision = json.loads(desired.read_text(encoding="utf-8"))["revision_id"]
        bundle = json.loads((root / revision / "proxy-bundle.json").read_text(encoding="utf-8"))
        configuration = bundle["configuration"]
        encoded = os.environ["PLATFORM_SECRET_ENCRYPTION_KEY"]
        key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        cipher = AESGCM(key)
        key_id = hashlib.sha256(key).hexdigest()[:16]
        for name, envelope in bundle["credentials"].items():
            if envelope["key_id"] != key_id:
                raise RuntimeError("platform_credential_key_mismatch")
            payload = base64.b64decode(envelope["ciphertext"])
            os.environ[name] = cipher.decrypt(payload[:12], payload[12:], envelope["aad"].encode()).decode()
    else:
        configuration = {"model_list": [], "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY", "database_url": "os.environ/LITELLM_DATABASE_URL"}}
    os.environ["STORE_MODEL_IN_DB"] = "True"
    with tempfile.TemporaryDirectory(prefix="insightforge-proxy-") as directory:
        path = Path(directory) / "litellm.yaml"
        path.write_text(yaml.safe_dump(configuration, allow_unicode=True), encoding="utf-8")
        child = subprocess.Popen(["litellm", "--config", str(path), "--host", "0.0.0.0", "--port", "4000"])
        def stop(_signum, _frame):
            child.terminate()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while child.poll() is None:
            if bundle:
                try:
                    with urlopen("http://127.0.0.1:4000/health/readiness", timeout=2) as response:
                        if response.status == 200 and child.poll() is None:
                            receipt = root / bundle["revision_id"] / "litellm.applied.json"
                            temporary = receipt.with_suffix(".tmp")
                            temporary.write_text(json.dumps({"revision_id": bundle["revision_id"], "fingerprint": bundle["fingerprint"],
                                "pid": child.pid, "time": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
                            temporary.replace(receipt)
                except (OSError, ValueError):
                    pass
            time.sleep(3)
        return child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
