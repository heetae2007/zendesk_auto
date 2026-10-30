import os
os.environ['ADMIN_PASSWORD']='test-password-not-for-production'
os.environ['DATA_DIR']='/tmp/zendesk-web-tests'
from fastapi.testclient import TestClient
from app.main import app

def test_auth_and_csrf():
    client=TestClient(app)
    assert client.get('/api/state').status_code==401
    auth=('admin',os.environ['ADMIN_PASSWORD'])
    r=client.get('/api/state',auth=auth);assert r.status_code==200
    assert r.json()['mode']=='추천 전용'
    assert client.post('/api/run',auth=auth,json={}).status_code==403
    assert client.get('/',auth=auth).status_code==200
    assert client.get('/static/app.js').status_code==200

def test_live_update_endpoint_absent():
    paths={r.path for r in app.routes}
    assert '/api/assign' not in paths and '/api/live' not in paths
