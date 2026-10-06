import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.request_security import boundary


class RequestBoundaryTest(unittest.TestCase):
    def setUp(self):
        app=FastAPI();app.middleware('http')(boundary)
        @app.post('/mutate')
        def mutate():return {'changed':True}
        @app.get('/view')
        def view():return {'changed':False}
        self.client=TestClient(app);self.addCleanup(self.client.close)

    def test_cookie_mutation_requires_same_origin_and_refuses_cross_site(self):
        self.client.cookies.set('openstocks_session','synthetic-session')
        for headers in ({},{'Origin':'https://attacker.invalid'},{'Origin':'null'},
                        {'Origin':'http://testserver','Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.client.post('/mutate',headers=headers).status_code,403)
        response=self.client.post('/mutate',headers={'Origin':'http://testserver'})
        self.assertEqual(response.status_code,200);self.assertEqual(response.headers['x-frame-options'],'DENY')
        self.assertEqual(self.client.get('/view').status_code,200)

    def test_header_proxy_spoof_does_not_make_an_origin_trusted(self):
        self.assertEqual(self.client.post('/mutate',headers={'Origin':'https://attacker.invalid',
            'X-Forwarded-Host':'attacker.invalid','X-Forwarded-Proto':'https'}).status_code,403)
