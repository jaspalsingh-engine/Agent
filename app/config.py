from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    apollo_api_key: str
    openai_api_key: str

    gmail_sender_address: str = ""
    gmail_credentials_path: str = "credentials/credentials.json"
    gmail_token_path: str = "credentials/token.json"

    your_name: str = "Jaspal Singh"
    your_title: str = "Director of Revenue Operations"
    your_company: str = "YourCompany"
    your_calendly_link: str = "https://calendly.com/yourlink"
    digest_email_recipient: str = ""

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
