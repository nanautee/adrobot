from datetime import datetime, timezone

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import DateTime, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=('.env', '.env.local'), extra='ignore')
    keitaro_url: str = 'https://your-tracker.example'
    keitaro_api_key: str = ''
    database_url: str = 'sqlite:///./adrobot.db'


settings = Settings()


class Base(DeclarativeBase):
    pass


class Operation(Base):
    __tablename__ = 'operations'
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    campaign_id: Mapped[int] = mapped_column(Integer)
    details: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


engine = create_engine(settings.database_url, connect_args={'check_same_thread': False} if settings.database_url.startswith('sqlite') else {})
Session = sessionmaker(engine)


class KeitaroError(Exception):
    def __init__(self, message, status=502):
        self.message, self.status = message, status


class Keitaro:
    def __init__(self, client):
        self.client = client

    async def request(self, method, path, **kwargs):
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.RequestError:
            raise KeitaroError('Keitaro недоступен. Результат записи может быть неизвестен; проверьте кампанию перед повтором.') from None
        if response.is_error:
            status = response.status_code if response.status_code in (400, 404, 409, 422) else 502
            raise KeitaroError(f'Keitaro вернул HTTP {response.status_code}. Проверьте доступы и параметры.', status)
        try:
            return response.json()
        except ValueError:
            raise KeitaroError('Keitaro вернул некорректный JSON.') from None


def distribute(ids):
    if not ids or len(ids) > 100:
        raise ValueError('В потоке должно быть от 1 до 100 активных офферов.')
    quotient, remainder = divmod(100, len(ids))
    return [{'offer_id': oid, 'share': quotient + (i < remainder), 'state': 'active'} for i, oid in enumerate(ids)]
