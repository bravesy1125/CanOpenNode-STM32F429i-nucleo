from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CANOPEN_")

    app_name: str = "CANopen WebUI"
    host: str = "0.0.0.0"
    port: int = 80
    bustype: str = "slcan"
    channel: str = "COM6"
    bitrate: int = 1_000_000
    node_ids: list[int] = Field(default_factory=list)
    poll_interval: float = 0.5
    mock: bool = False
    eds_path: Path = Path("../../../canopen-python-test/canopentest.eds")

    @field_validator("node_ids", mode="before")
    @classmethod
    def parse_node_ids(cls, value):
        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        return value


settings = Settings()
