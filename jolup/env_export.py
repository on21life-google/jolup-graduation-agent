"""Serialize credentials only for an explicitly requested local download."""
from __future__ import annotations

import re

from jolup.llm import resolve_config


def export_dotenv(config: dict) -> bytes:
    """Do not read a server API key, write a file, log values, or call an API."""
    key = str(config.get("api_key") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,2048}", key):
        raise ValueError("현재 입력란의 API 키를 확인해 주세요. 공백이나 환경파일 특수문자는 내보낼 수 없습니다.")
    if not str(config.get("model") or "").strip():
        raise ValueError("내보낼 모델 ID를 먼저 입력해 주세요.")
    info = resolve_config(config)
    if not info["ok"]:
        raise ValueError(info["error"])
    lines = [
        "# Contains a real API key. Share privately with the intended teammate only.",
        "# Docker Compose reads this file. Python launchers do not load it automatically.",
        f"JOLUP_API_KEY={key}",
        f"JOLUP_LLM_PROVIDER={info['provider']}",
        f"JOLUP_LLM_MODEL={info['model']}",
    ]
    workspace = str(config.get("workspace_id") or "").strip()
    if info["provider"] == "anthropic" and workspace:
        lines.append(f"ANTHROPIC_WORKSPACE_ID={workspace}")
    return ("\n".join(lines) + "\n").encode("utf-8")
