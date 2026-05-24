"""Runtime configuration. .env via pydantic-settings, YAML for profile / companies / qa_bank / blocklist."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    autoapply_home: Path = Path("./data")
    config_dir: Path = Path("./config")
    resume_pdf: Path = Path("./config/resume.pdf")

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "autoapply"

    gmail_client_secrets: Path = Path("./config/gmail_credentials.json")
    gmail_token_path: Path = Path("./data/gmail_token.json")
    gmail_label_unprocessed: str = "JobAlerts/Unprocessed"
    gmail_label_processed: str = "JobAlerts/Processed"
    gmail_label_applied_out: str = "Applied/Sent"
    gmail_label_reply: str = "Applied/Reply"
    gmail_user_email: str = ""
    gmail_sender_name: str = ""

    sarvam_api_key: SecretStr | None = None

    scorer_provider: str = "gemini"
    scorer_model: str = "gemini/gemini-2.0-flash"
    scorer_api_key: SecretStr | None = None
    scorer_service_account_json: Path | None = None
    scorer_google_cloud_location: str = "us-central1"

    composer_provider: str = "groq"
    composer_model: str = "groq/llama-3.3-70b-versatile"
    composer_api_key: SecretStr | None = None

    agent_provider: str = "gemini"
    agent_model: str = "gemini/gemini-2.0-flash"
    agent_api_key: SecretStr | None = None
    agent_service_account_json: Path | None = None
    agent_google_cloud_location: str = "us-central1"

    linkedin_browser_dir: Path = Path("./data/linkedin-profile")
    linkedin_scroll_pages: int = 5
    linkedin_max_posts_per_query: int = 25
    linkedin_max_jobs_per_search: int = 12
    linkedin_fetch_post_pages: bool = True
    linkedin_fetch_job_pages: bool = True
    linkedin_jobs_tpr: str = "r604800"  # past week on LinkedIn Jobs
    linkedin_agent_fallback_min: int = 3  # run LLM agent if DOM finds fewer than this
    web_discovery_enabled: bool = True  # Google Jobs open-web search
    scraper_llm_enrich: bool = True  # LLM extracts emails + job details from page text
    naukri_browser_dir: Path = Path("./data/naukri-profile")
    foundit_browser_dir: Path = Path("./data/foundit-profile")
    instahyre_browser_dir: Path = Path("./data/instahyre-profile")
    # Which portal scrapers to run (all enabled by default)
    scraper_portals: list[str] = ["linkedin", "naukri", "foundit", "instahyre"]
    # When false, skip Greenhouse/Lever (requires config/companies.yaml slugs).
    ingest_ats_sources: bool = False

    # OpenAI-compatible provider (also covers Ollama: set base_url to http://localhost:11434/v1)
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"

    daily_send_cap: int = 10
    fit_threshold: float = 6.5
    auto_apply_threshold: float = 9.5   # jobs at or above this score bypass review and apply instantly
    cooldown_days: int = 30
    timezone: str = "Asia/Kolkata"
    log_level: str = "INFO"
    headless_browser: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("scorer_service_account_json", "agent_service_account_json", mode="before")
    @classmethod
    def _blank_path_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def browser_profile_dir(self) -> Path:
        return self.autoapply_home / "browser-profile"

    @property
    def logs_dir(self) -> Path:
        return self.autoapply_home / "logs"

    def ensure_dirs(self) -> None:
        for d in (
            self.autoapply_home,
            self.browser_profile_dir,
            self.logs_dir,
            self.naukri_browser_dir,
            self.foundit_browser_dir,
            self.instahyre_browser_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


# === Profile YAML models ==================================================


class Identity(BaseModel):
    full_name: str
    email_primary: str
    phone: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    location: str = ""
    willing_to_relocate: list[str] = Field(default_factory=list)


class Career(BaseModel):
    years_experience: int = 0
    current_title: str = ""
    current_company: str = ""
    notice_period_days: int = 30
    current_ctc_inr: int = 0
    expected_ctc_inr: int = 0
    work_authorisation: str = ""


class Target(BaseModel):
    roles: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    industries_priority: list[str] = Field(default_factory=list)
    industries_avoid: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    on_site_cities_ok: list[str] = Field(default_factory=list)
    salary_min_inr: int = 0
    deal_breakers: list[str] = Field(default_factory=list)


class Skills(BaseModel):
    primary: list[str] = Field(default_factory=list)
    secondary: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    identity: Identity
    career: Career
    target: Target
    skills: Skills
    highlight_bullets: list[str] = Field(default_factory=list)
    always_apply: list[str] = Field(default_factory=list)
    qa_overrides: dict[str, str] = Field(default_factory=dict)

    def for_prompt(self) -> dict:
        """Compact dict suitable for LLM context."""
        return self.model_dump(exclude={"qa_overrides"})


class ATSCompany(BaseModel):
    slug: str
    label: str


class CompaniesConfig(BaseModel):
    greenhouse: list[ATSCompany] = Field(default_factory=list)
    lever: list[ATSCompany] = Field(default_factory=list)
    ashby: list[ATSCompany] = Field(default_factory=list)


class Blocklist(BaseModel):
    companies: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    notes: str = ""


class PortalCredentials(BaseModel):
    """Per-portal login credentials. Stored in config/portals.yaml (gitignored)."""
    email: str = ""
    password: str = ""

    @property
    def is_set(self) -> bool:
        return bool(self.email and self.password)


class PortalsConfig(BaseModel):
    naukri: PortalCredentials = Field(default_factory=PortalCredentials)
    foundit: PortalCredentials = Field(default_factory=PortalCredentials)
    instahyre: PortalCredentials = Field(default_factory=PortalCredentials)


# === Loaders ==============================================================


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing config file: {path}. Create it under config/ and fill in your details."
        )
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


@lru_cache(maxsize=1)
def get_profile() -> Profile:
    s = get_settings()
    return Profile.model_validate(_load_yaml(s.config_dir / "profile.yaml"))


@lru_cache(maxsize=1)
def get_companies() -> CompaniesConfig:
    s = get_settings()
    path = s.config_dir / "companies.yaml"
    if not path.exists():
        return CompaniesConfig()
    return CompaniesConfig.model_validate(_load_yaml(path))


@lru_cache(maxsize=1)
def get_blocklist() -> Blocklist:
    s = get_settings()
    path = s.config_dir / "blocklist.yaml"
    if not path.exists():
        return Blocklist()
    return Blocklist.model_validate(_load_yaml(path))


def get_portals_config() -> PortalsConfig:
    """Load portal credentials from config/portals.yaml. Not lru_cached — credentials change at runtime via the UI."""
    s = get_settings()
    path = s.config_dir / "portals.yaml"
    if not path.exists():
        return PortalsConfig()
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return PortalsConfig.model_validate(raw)


def save_portals_config(cfg: PortalsConfig) -> None:
    s = get_settings()
    path = s.config_dir / "portals.yaml"
    path.write_text(
        yaml.dump(cfg.model_dump(), default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


@lru_cache(maxsize=1)
def get_qa_bank() -> dict[str, str]:
    s = get_settings()
    path = s.config_dir / "qa_bank.yaml"
    if not path.exists():
        return {}
    raw = _load_yaml(path)
    return {k.lower(): str(v) for k, v in raw.items()}
