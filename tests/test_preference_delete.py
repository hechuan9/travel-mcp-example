import asyncio
import httpx
from travel_ai.store import Store
from travel_ai.tools import HelpInput, ToolHandler, schema_help


def test_preference_delete_is_discoverable_and_scoped(tmp_path):
    store = Store(tmp_path / 'test.sqlite3')
    session = store.create_session('owner', 'test')
    run, _ = store.create_run('owner', session['id'], 'delete', 'test')
    seen = []
    def respond(request):
        seen.append(request)
        if request.url.path == '/openapi.json':
            return httpx.Response(200, json={'paths': {'/api/preferences/{member}/{key}': {'delete': {}}, '/api/bookings/{id}': {'delete': {}}}})
        return httpx.Response(204)
    async def scenario():
        async with httpx.AsyncClient(base_url='http://business', transport=httpx.MockTransport(respond)) as client:
            help_result = await schema_help(client, HelpInput(resource='preferences', method='delete'))
            assert 'delete' in str(help_result)
            assert 'delete' not in str(await schema_help(client, HelpInput(resource='bookings', method='delete')))
            tool = ToolHandler(store, 'owner', run['id'], client)
            assert (await tool('travel_api', {'method': 'DELETE', 'path': '/preferences/member-a/dining', 'body': {'version': 2}}))[1]
            count = len(seen)
            assert not (await tool('travel_api', {'method': 'DELETE', 'path': '/bookings/booking-a', 'body': {'version': 1}}))[1]
            assert len(seen) == count
            assert seen[-1].method == 'DELETE' and seen[-1].content == b'{"version":2}'
    asyncio.run(scenario())
