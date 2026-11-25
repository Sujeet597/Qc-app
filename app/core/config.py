from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Garment QC Video Inspection"
    database_url: str = "sqlite:///./qc.db"
    # False = no login: the page picks a role (X-Role header) and acts as that demo user. Testing only!
    auth_enabled: bool = False
    jwt_secret: str = "change-me"
    jwt_expire_minutes: int = 480
    storage_dir: str = "./storage"

    ai_provider: str = "mock"  # "mock" | "claude"
    anthropic_model: str = "claude-opus-5-5"
    ai_confidence_threshold: float = 0.80

    color_delta_e_max: float = 3.0

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    seed_demo_data: bool = True


settings = Settings()
