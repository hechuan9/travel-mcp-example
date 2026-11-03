import json
import httpx
from test_mcp import setup, call, value, BUSINESS


def test_credit_registration_is_direct_scoped_and_read_back(setup):
    client, state = setup
    records = {}
    def credits(request):
        assert request.headers['authorization'] == BUSINESS
        if request.url.path == '/api/settlements':
            return httpx.Response(200, json=[{'id':'credit-settlement','kind':'credit'}, {'id':'refund','kind':'refund'}])
        assert request.url.path.startswith('/api/travel-credits')
        if request.method == 'POST':
            body = json.loads(request.content)
            assert len(body.pop('request_id')) == 64
            records['saved'] = dict(id='saved', version=1, **body)
            state['writes'].append(request.url.path)
        elif request.method == 'PATCH':
            records['saved'].update(json.loads(request.content))
            records['saved']['version'] += 1
            state['writes'].append(request.url.path)
        return httpx.Response(200, json={'items':list(records.values()), 'total':len(records)} if request.url.path == '/api/travel-credits' and request.method == 'GET' else records['saved'])
    state['business_response'] = credits
    listed = value(call(client, 'travel_list_credits', {'status':'available','expiry':'30'}))
    assert listed['items'] == [] and listed['credit_settlements'] == [{'id':'credit-settlement','kind':'credit'}]
    fields = {'owner_id':'member','provider':'Synthetic Air','code':'SYN123','original_amount':'100.00','remaining_amount':'100.00','currency':'USD','evidence':'Synthetic issued credit'}
    result = value(call(client, 'travel_save_credit', {'request_id':'create-credit','credit':fields}))
    assert result['status'] == 'completed' and result['changes'][0]['verification']['status'] == 'verified'
    updated = value(call(client, 'travel_save_credit', {'request_id':'update-credit','credit_id':'saved','version':1,'credit':{'remaining_amount':'50.00','evidence':'Synthetic usage confirmation'}}))
    assert updated['status'] == 'completed' and updated['changes'][0]['after']['remaining_amount'] == '50.00'
    stale = value(call(client, 'travel_save_credit', {'request_id':'stale-credit','credit_id':'saved','version':1,'credit':{'evidence':'Synthetic stale confirmation'}}))
    assert stale['status'] == 'conflict' and len(state['writes']) == 2
    for args in ({'request_id':'bad','credit':{'evidence':'Synthetic'}}, {'request_id':'bad','credit':{**fields,'path':'/bookings'}}, {'request_id':'bad','credit_id':'saved','credit':fields}):
        assert call(client, 'travel_save_credit', args)['isError']
    assert len(state['writes']) == 2


def test_credit_registration_does_not_retry_unknown_outcome(setup):
    client, state = setup
    def lost(request):
        state['writes'].append(request.url.path)
        raise httpx.ReadTimeout('Synthetic lost response', request=request)
    state['business_response'] = lost
    fields = {'owner_id':'member','provider':'Synthetic Air','code':'SYN123','original_amount':'100','remaining_amount':'100','currency':'USD','evidence':'Synthetic issued credit'}
    result = value(call(client, 'travel_save_credit', {'request_id':'lost-credit','credit':fields}))
    assert result['status'] == 'outcome_unknown' and len(state['writes']) == 1
