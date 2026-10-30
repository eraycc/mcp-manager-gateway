"""Application configuration; paths are independent of the launch directory."""
import base64
import hashlib
import os
import re
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet
from filelock import FileLock
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
# Source-tree metadata is never used for installed runtime assets or user data.
PROJECT_ROOT = PACKAGE_ROOT.parents[1]


def default_home():
    return Path(os.environ.get("MCP_MANAGER_HOME", Path.home() / ".mcp-manager")).expanduser().resolve()


def normalize_database_url(value: str, base_dir: Path) -> str:
    if value.startswith("mysql://"):
        return "mysql+asyncmy://" + value[8:]
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///", "sqlite://"):
        if value.startswith(prefix):
            name = value[len(prefix):].replace("\\", "/")
            if name == ":memory:":
                return "sqlite+aiosqlite:///:memory:"
            # Windows drive paths must not be interpreted as relative on Linux.
            if not re.match(r"^[A-Za-z]:/", name):
                name = (base_dir / name).resolve().as_posix()
            return "sqlite+aiosqlite:///" + name
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    home_dir: Path
    data_dir: Path = Path(".")
    database_url: str = ""
    storage_migration_mode: Literal["auto", "manual"] = "auto"
    host: str = "127.0.0.1"
    port: int = 8765
    secret_key: str = ""
    cookie_secure: bool = False
    public_url: str = "http://127.0.0.1:8765"
    runtime_global_calls: int = 64
    runtime_user_calls: int = 16
    runtime_mcp_calls: int = 16
    runtime_queued_calls: int = 256
    runtime_global_instances: int = 64
    runtime_user_instances: int = 16
    runtime_instance_waiters: int = 64
    runtime_queue_timeout: float = 60.0
    password_workers: int = 2
    password_waiters: int = 32
    login_failure_limit: int = 20
    login_failure_window: float = 300.0
    registration_limit: int = 100
    registration_window: float = 3600.0
    identity_rate_limit_entries: int = 10000

    def __init__(self, **values):
        home = Path(values.get("home_dir") or default_home()).expanduser().resolve()
        values["home_dir"] = home
        values.setdefault("_env_file", home / ".env")
        super().__init__(**values)

    @model_validator(mode="after")
    def prepare(self):
        self.data_dir = (self.home_dir / self.data_dir.expanduser()).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = "sqlite:///" + (self.data_dir / "mcp-manager.sqlite").as_posix()
        self.database_url = normalize_database_url(self.database_url, self.data_dir)
        if not self.secret_key:
            path = self.data_dir / "secret.key"
            with FileLock(str(path) + ".lock"):
                if not path.exists():
                    with path.open("xb") as f:
                        f.write(Fernet.generate_key())
                    path.chmod(0o600)
                self.secret_key = path.read_text(encoding="utf-8").strip()
        if not self.secret_key:
            raise ValueError("secret_key must not be empty")
        return self

    @property
    def jwt_secret(self) -> str:
        return hashlib.sha256(("mcp-jwt:" + self.secret_key).encode()).hexdigest()

    @property
    def encryption_key(self) -> bytes:
        return base64.urlsafe_b64encode(hashlib.sha256(("mcp-encryption:" + self.secret_key).encode()).digest())
