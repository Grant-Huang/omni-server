"""Central config loading. Everything that reads an env var does it through here,
not scattered across modules -- see docs/mvp-plan.md section 2.2: the day this needs
to grow into per-family/BYOK credentials instead of one .env, this is the one place
that changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

QWEN_WS_BASE_SHARED_DEFAULT = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"

# Same shortlist as workforce/web-demo/server.py's VOICE_OPTIONS (2026-08-23) -- every
# entry individually verified via a session.update round-trip against
# qwen3.5-omni-flash-realtime + the workspace domain. Kept in sync by hand for now;
# if it drifts, workforce's copy is the one with the actual verification history.
VOICE_OPTIONS = [
    {"id": "Serena", "label": "Serena（女，温柔，默认）"},
    {"id": "Tina", "label": "Tina（女，甜美，官方默认）"},
    {"id": "Sunnybobi", "label": "Sunnybobi（女，大大咧咧的社恐邻家姑娘）"},
    {"id": "Ethan", "label": "Ethan（男，标准普通话）"},
    {"id": "Raymond", "label": "Raymond（男，清亮）"},
    {"id": "Dylan", "label": "Dylan（男，北京话）"},
]


@dataclass(frozen=True)
class Config:
    qwen_api_key: str
    qwen_workspace_id: str
    qwen_ws_base_shared: str
    qwen_model: str
    qwen_voice: str
    qwen_text_model: str
    host: str
    port: int
    cors_origins: tuple
    memory_path: Path

    @property
    def upstream_ws_base(self) -> str:
        # Workspace-specific domain is what actually fixed the session.update hangs
        # (see omni-server/docs/design-risks-review.md section 3.2 and workforce's
        # web-demo README "重大突破" 2026-08-23) -- prefer it whenever configured.
        if self.qwen_workspace_id:
            return f"wss://{self.qwen_workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
        return self.qwen_ws_base_shared

    @property
    def compatible_mode_base(self) -> str:
        if self.qwen_workspace_id:
            return f"https://{self.qwen_workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"


def load_config() -> Config:
    cors_raw = os.environ.get("CORS_ORIGINS", "*")
    cors_origins = tuple(o.strip() for o in cors_raw.split(",") if o.strip())
    return Config(
        qwen_api_key=os.environ.get("QWEN_API_KEY", ""),
        qwen_workspace_id=os.environ.get("QWEN_WORKSPACE_ID", ""),
        qwen_ws_base_shared=os.environ.get("QWEN_WS_BASE", QWEN_WS_BASE_SHARED_DEFAULT),
        qwen_model=os.environ.get("QWEN_MODEL", "qwen3.5-omni-flash-realtime"),
        qwen_voice=os.environ.get("QWEN_VOICE", "Serena"),
        qwen_text_model=os.environ.get("QWEN_TEXT_MODEL", "qwen-turbo"),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8766")),
        cors_origins=cors_origins,
        memory_path=Path(os.environ.get("MEMORY_DATA_PATH", str(BASE_DIR / "data" / "memory.json"))),
    )
