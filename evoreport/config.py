from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    database_path: Path
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_model: str = "glm-5-3-flash-260828"
    ark_api_key: str = ""
    artifact_path: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        project_root = Path(__file__).resolve().parent.parent
        database_value = os.getenv("EVOREPORT_DATABASE_PATH", "").strip()
        database_path = Path(database_value).expanduser() if database_value else project_root / "evoreport.db"
        artifact_value = os.getenv("EVOREPORT_ARTIFACT_PATH", "").strip()
        artifact_path = Path(artifact_value).expanduser() if artifact_value else project_root
        return cls(
            host=os.getenv("EVOREPORT_HOST", "127.0.0.1"),
            port=int(os.getenv("EVOREPORT_PORT", "8090")),
            database_path=database_path.resolve(),
            ark_base_url=os.getenv("EVOREPORT_ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/"),
            ark_model=os.getenv("EVOREPORT_ARK_MODEL", "glm-5-3-flash-260828").strip(),
            ark_api_key=os.getenv("ARK_API_KEY", "").strip(),
            artifact_path=artifact_path.resolve(),
        )
