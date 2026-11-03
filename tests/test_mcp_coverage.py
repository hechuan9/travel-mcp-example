"""Exercise exact collection selectors, version gates and proposal boundaries."""
import asyncio

import httpx
import pytest
from pydantic import ValidationError

from travel_ai.mcp_server import Change, ReadOnlyTools, read_record
from travel_ai.tools import ROOTS


@pytest.mark.parametrize('resource', ROOTS)
def test_read_only_all_existing_business_resources(resource):
    calls = []
    async def delegate(name, args):
        calls.append(name)
        return {}, True
    handler = ReadOnlyTools(delegate)
    assert asyncio.run(handler('api_help', {'resource': resource}))[1]
    assert asyncio.run(handler('travel_api', {'method': 'GET', 'path': '/' + resource}))[1]
    assert not asyncio.run(handler('travel_api', {'method': 'POST', 'path': '/' + resource}))[1]
    assert calls == ['api_help', 'travel_api']


@pytest.mark.parametrize('method,path,body', [
    ('DELETE', '/ideas/i', {'version': 1}),
    ('PATCH', '/ideas/i', {}),
    ('POST', '/plans/p/decision', {'version': 1}),
    ('POST', '/connections/plaid/link', {}),
    ('PUT', '/imports/records/r/travel-account', {}),
    ('DELETE', '/trips/t/bookings/b', {'version': 1}),
    ('PATCH', '/cards/c', {'version': True}),
    ('POST', '/bookings/b/cancel', {}),
    ('POST', '/rewards/tasks/r/resolve', {}),
    ('PUT', '/preferences/m/k?other=value', {'version': 0}),
])
def test_unsupported_or_unversioned_writes_rejected(method, path, body):
    with pytest.raises(ValidationError):
        Change(method=method, path=path, body=body, reason='synthetic')


@pytest.mark.parametrize('method,path,body,get_path,payload,expected', [
    ('PATCH', '/ideas/i', {'version': 2}, '/api/ideas/i', {'id': 'i', 'version': 2}, {'id': 'i', 'version': 2}),
    ('PATCH', '/cards/c', {'version': 2}, '/api/cards', {'cards': [{'id': 'c', 'version': 2}]}, {'id': 'c', 'version': 2}),
    ('PUT', '/cards/c/benefits/b/usage', {'version': 0}, '/api/cards', {'cards': [{'id': 'c', 'benefits': [{'key': 'b', 'usage_version': 0}]}]}, {'key': 'b', 'usage_version': 0}),
    ('PATCH', '/hotels/h', {'version': 3}, '/api/hotels', [{'id': 'h', 'version': 3}], {'id': 'h', 'version': 3}),
    ('POST', '/bookings/b/cancel', {'version': 1}, '/api/bookings', [{'id': 'b', 'version': 1}], {'id': 'b', 'version': 1}),
    ('POST', '/rewards/tasks/r/resolve', {'version': 1}, '/api/rewards', {'tasks': [{'id': 'r', 'version': 1}]}, {'id': 'r', 'version': 1}),
    ('PUT', '/preferences/m/k', {'version': 0}, '/api/preferences?member_id=m', {'preferences': []}, None),
    ('DELETE', '/preferences/m/k', {'version': 2}, '/api/preferences?member_id=m', {'preferences': [{'key': 'k', 'version': 2}]}, {'key': 'k', 'version': 2}),
    ('PUT', '/exchange-rates/EUR', {'version': 0}, '/api/exchange-rates', [], None),
    ('PUT', '/trips/t/bookings/b', {'version': 4}, '/api/trips/t', {'id': 't', 'version': 4}, {'id': 't', 'version': 4}),
])
def test_reads_actual_api_shape_for_optimistic_lock(method, path, body, get_path, payload, expected):
    plan = Change(method=method, path=path, body=body, reason='synthetic').model_dump()
    def upstream(request):
        assert request.method == 'GET'
        assert str(request.url).removeprefix('http://business') == get_path
        return httpx.Response(200, json=payload)
    async def check():
        async with httpx.AsyncClient(base_url='http://business', transport=httpx.MockTransport(upstream)) as client:
            _, observed = await read_record(client, plan)
            assert observed == expected
    asyncio.run(check())


@pytest.mark.parametrize('path,get_path,payload', [
    ('/ideas', '/api/ideas/new', {'id': 'new'}),
    ('/travel-credits/cleanups', '/api/travel-credits/cleanups/new', {'id': 'new'}),
    ('/bookings', '/api/bookings', [{'id': 'new'}]),
    ('/trips/t/bookings', '/api/bookings', [{'id': 'new'}]),
    ('/trips/t/plans', '/api/plans', [{'id': 'new'}]),
    ('/bookings/b/settlements', '/api/settlements', [{'id': 'new'}]),
    ('/rewards/certificates', '/api/rewards', {'certificates': [{'id': 'new'}]}),
    ('/hotels', '/api/hotels', [{'id': 'new'}]),
    ('/travel-accounts', '/api/travel-accounts', [{'id': 'new'}]),
])
def test_creation_verifies_returned_id_in_canonical_collection(path, get_path, payload):
    plan = Change(method='POST', path=path, body={}, reason='synthetic').model_dump()
    def upstream(request):
        assert request.url.path == get_path
        return httpx.Response(200, json=payload)
    async def check():
        async with httpx.AsyncClient(base_url='http://business', transport=httpx.MockTransport(upstream)) as client:
            _, observed = await read_record(client, plan, {'id': 'new'})
            assert observed == {'id': 'new'}
    asyncio.run(check())
