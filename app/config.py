from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    apollo_api_key: str
    openai_api_key: str
    resend_api_key: str

    resend_from_email: str = "onboarding@resend.dev"   # replace once domain verified
    digest_email_recipient: str = ""

    your_name: str = "Jaspal Singh"
    your_title: str = "Director of Revenue Operations"
    your_company: str = "YourCompany"
    your_calendly_link: str = "https://calendly.com/yourlink"

    app_host: str = "localhost"
    app_port: int = 8000
    secret_token: str = "change-me"

    target_industries: str = (
        "Construction,Consulting,Staffing and Recruiting,Oil and Gas,"
        "Financial Services,Information Technology and Services,"
        "Computer Software,Management Consulting"
    )
    min_employees: int = 20
    accounts_per_week: int = 50

    @property
    def industry_list(self) -> List[str]:
        return [i.strip() for i in self.target_industries.split(",")]

    @property
    def dashboard_url(self) -> str:
        return f"http://{self.app_host}:{self.app_port}"

    class Config:
        env_file = ".env"


settings = Settings()
