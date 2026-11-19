import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pycountry
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.core import Keitaro, KeitaroError, Operation, Session, distribute, settings


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(base_url=settings.keitaro_url.rstrip('/') + '/admin_api/v1/', headers={'Api-Key': settings.keitaro_api_key}, timeout=20) as client:
        app.state.keitaro = Keitaro(client)
        app.state.lock = asyncio.Lock()
        yield


app = FastAPI(title='Adrobot · Keitaro wrapper', lifespan=lifespan)


@app.exception_handler(KeitaroError)
async def upstream_error(request, exc):
    return JSONResponse({'detail': exc.message}, status_code=exc.status)


class CampaignInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    country: str
    offer_id: int = Field(gt=0)
    domain_id: int = Field(gt=0)
    group_id: int = Field(gt=0)
    traffic_source_id: int = Field(gt=0)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError('Укажите название')
        return value.strip()

    @field_validator('country')
    @classmethod
    def country_code(cls, value):
        value = value.strip().upper()
        if len(value) != 2 or not pycountry.countries.get(alpha_2=value):
            raise ValueError('Нужен ISO 3166-1 alpha-2 код страны')
        return value


class OfferInput(BaseModel):
    offer_id: int = Field(gt=0)


def audit(kind, campaign_id, details):
    try:
        with Session.begin() as session:
            session.add(Operation(kind=kind, campaign_id=campaign_id, details=details))
    except Exception:
        raise HTTPException(500, 'Keitaro обновлён, но журнал БД недоступен. Не повторяйте запись: проверьте кампанию.') from None


@app.get('/', include_in_schema=False)
def index():
    return FileResponse(Path(__file__).parent / 'index.html')


@app.get('/api/catalog/{resource}')
async def catalog(resource: str, request: Request):
    if resource not in ('campaigns', 'offers', 'domains', 'groups', 'traffic_sources'):
        raise HTTPException(404)
    params = {'type': 'campaigns'} if resource == 'groups' else {}
    return await request.app.state.keitaro.request('GET', resource, params=params)


@app.get('/api/campaigns/{campaign_id}/streams')
async def streams(campaign_id: int, request: Request):
    return await request.app.state.keitaro.request('GET', f'campaigns/{campaign_id}/streams')


@app.post('/api/campaigns', status_code=201)
async def create_campaign(data: CampaignInput, request: Request):
    api = request.app.state.keitaro
    await api.request('GET', f'offers/{data.offer_id}')
    campaign = await api.request('POST', 'campaigns', json={
        'name': data.name, 'alias': 'adrobot-' + uuid4().hex[:12],
        'domain_id': data.domain_id, 'group_id': str(data.group_id),
        'traffic_source_id': data.traffic_source_id, 'type': 'position', 'state': 'disabled',
    })
    cid = campaign['id']
    try:
        await api.request('POST', 'streams', json={
            'campaign_id': cid, 'name': f'GEO {data.country} → Google',
            'type': 'regular', 'position': 1, 'schema': 'redirect',
            'action_type': 'http', 'action_payload': 'https://www.google.com/',
            'filters': [{'name': 'country', 'mode': 'accept', 'payload': [data.country]}],
            'state': 'active', 'collect_clicks': True,
        })
        await api.request('POST', 'streams', json={
            'campaign_id': cid, 'name': 'Офферы · остальные страны',
            'type': 'default', 'position': 2, 'schema': 'landings', 'action_type': 'http',
            'offers': distribute([data.offer_id]), 'state': 'active', 'collect_clicks': True,
        })
        campaign = await api.request('PUT', f'campaigns/{cid}', json={'state': 'active'})
    except KeitaroError as exc:
        raise KeitaroError(f'Не удалось завершить кампанию #{cid}. Она создана выключенной; проверьте её в Keitaro. {exc.message}') from None
    audit('create', cid, data.name)
    return campaign


async def edit_offers(cid, sid, oid, request, remove=False):
    api = request.app.state.keitaro
    async with request.app.state.lock:
        stream = await api.request('GET', f'streams/{sid}')
        if int(stream['campaign_id']) != cid:
            raise HTTPException(404, 'Поток не принадлежит кампании')
        if stream.get('schema') != 'landings':
            raise HTTPException(409, 'Этот поток не использует офферы')
        offers = stream.get('offers', [])
        if any(o.get('state', 'active') != 'active' for o in offers):
            raise HTTPException(409, 'В потоке есть выключенные офферы. Измените их в Keitaro перед редактированием.')
        ids = [int(o['offer_id']) for o in offers]
        if remove:
            if oid not in ids:
                raise HTTPException(404, 'Оффер отсутствует в потоке')
            ids.remove(oid)
        else:
            if oid in ids:
                raise HTTPException(409, 'Оффер уже добавлен')
            await api.request('GET', f'offers/{oid}')
            ids.append(oid)
        try:
            updated = distribute(ids)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        result = await api.request('PUT', f'streams/{sid}', json={'offers': updated})
        audit('remove_offer' if remove else 'add_offer', cid, f'stream={sid}; offer={oid}')
        return result


@app.post('/api/campaigns/{cid}/streams/{sid}/offers')
async def add_offer(cid: int, sid: int, data: OfferInput, request: Request):
    return await edit_offers(cid, sid, data.offer_id, request)


@app.delete('/api/campaigns/{cid}/streams/{sid}/offers/{oid}')
async def remove_offer(cid: int, sid: int, oid: int, request: Request):
    return await edit_offers(cid, sid, oid, request, remove=True)


@app.get('/api/operations')
def operations():
    with Session() as session:
        rows = session.scalars(select(Operation).order_by(Operation.id.desc()).limit(50))
        return [{'id': r.id, 'kind': r.kind, 'campaign_id': r.campaign_id, 'details': r.details, 'created_at': r.created_at} for r in rows]
