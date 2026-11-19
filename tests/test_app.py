from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core import Keitaro, KeitaroError, distribute
from app.main import app
import httpx
import asyncio


@pytest.mark.parametrize('count', range(1, 101))
def test_distribution(count):
    shares = [o['share'] for o in distribute(list(range(count)))]
    assert sum(shares) == 100
    assert max(shares) - min(shares) <= 1
    assert min(shares) > 0


@pytest.fixture
def client():
    with TestClient(app) as client:
        app.state.keitaro = AsyncMock()
        with patch('app.main.audit'):
            yield client


def test_create(client):
    app.state.keitaro.request.side_effect = [{'id': 1}, {'id': 42}, {}, {}, {'id': 42, 'state': 'active'}]
    r = client.post('/api/campaigns', json={'name': 'Demo', 'country': 'de', 'offer_id': 1, 'domain_id': 2, 'group_id': 3, 'traffic_source_id': 4})
    assert r.status_code == 201
    calls = app.state.keitaro.request.call_args_list
    assert calls[1].kwargs['json']['state'] == 'disabled'
    assert calls[2].kwargs['json']['filters'][0]['payload'] == ['DE']
    assert calls[2].kwargs['json']['action_payload'] == 'https://www.google.com/'
    assert calls[3].kwargs['json']['type'] == 'default'


def test_invalid_country(client):
    r = client.post('/api/campaigns', json={'name': 'Demo', 'country': 'ZZ', 'offer_id': 1, 'domain_id': 2, 'group_id': 3, 'traffic_source_id': 4})
    assert r.status_code == 422
    app.state.keitaro.request.assert_not_called()


def test_partial_create(client):
    app.state.keitaro.request.side_effect = [{}, {'id': 42}, KeitaroError('error')]
    r = client.post('/api/campaigns', json={'name': 'Demo', 'country': 'DE', 'offer_id': 1, 'domain_id': 2, 'group_id': 3, 'traffic_source_id': 4})
    assert r.status_code == 502
    assert '#42' in r.json()['detail']
    assert app.state.keitaro.request.call_count == 3


def stream(ids=(1,), **extra):
    return {'campaign_id': 10, 'schema': 'landings', 'offers': distribute(list(ids)), **extra}


def test_add(client):
    app.state.keitaro.request.side_effect = [stream(), {}, stream((1, 2))]
    assert client.post('/api/campaigns/10/streams/20/offers', json={'offer_id': 2}).status_code == 200
    assert app.state.keitaro.request.call_args.kwargs['json']['offers'] == distribute([1, 2])


@pytest.mark.parametrize('payload,status', [(stream(campaign_id=11), 404), (stream(schema='redirect'), 409), (stream((1, 2)), 409)])
def test_add_guards(client, payload, status):
    app.state.keitaro.request.return_value = payload
    assert client.post('/api/campaigns/10/streams/20/offers', json={'offer_id': 2}).status_code == status
    assert app.state.keitaro.request.call_count == 1


def test_remove_last(client):
    app.state.keitaro.request.return_value = stream()
    assert client.delete('/api/campaigns/10/streams/20/offers/1').status_code == 409
    assert app.state.keitaro.request.call_count == 1


def test_remove_redistributes(client):
    app.state.keitaro.request.side_effect = [stream((1, 2, 3, 4)), {}]
    assert client.delete('/api/campaigns/10/streams/20/offers/2').status_code == 200
    assert app.state.keitaro.request.call_args.kwargs['json']['offers'] == distribute([1, 3, 4])


def test_upstream_error():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))) as client:
            with pytest.raises(KeitaroError):
                await Keitaro(client).request('GET', 'https://example.com')
    asyncio.run(run())
