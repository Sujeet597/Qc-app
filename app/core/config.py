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
    anthropic_api_key: str = ""        # read from .env here: the Anthropic SDK itself does not read .env
    anthropic_model: str = "claude-opus-5-5"
    ai_timeout_s: float = 60.0         # per AI call; a hung call must not block the inspection
    ai_max_retries: int = 2            # SDK retries on network errors, 429 and 5xx
    ai_confidence_threshold: float = 0.80
    # BRD section 10: add automated decisions only after validation against real inspections. While false,
    # an AI Pass / Fail is shown to QC as a suggestion (Manual Review). Rule-based results (barcode match,
    # Delta-E, measurements, counts, non-nominated supplier code) are not affected.
    ai_auto_decide: bool = False

    color_delta_e_max: float = 3.0

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    seed_demo_data: bool = True


settings = Settings()
