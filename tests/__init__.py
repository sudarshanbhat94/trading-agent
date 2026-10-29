"""Disposable test runtime, before dataclass settings evaluate their defaults.

The real application must never import this package. HTTP transport fixtures
can use loopback; unmocked external calls are refused, not sent to a broker,
notification provider or production host. Test-created accounts stay private.
"""
import atexit
import os
from pathlib import Path
import tempfile

_runtime=tempfile.TemporaryDirectory(prefix='openstocks-tests-')
atexit.register(_runtime.cleanup)
_root=Path(_runtime.name)
for _name,_relative in {
    'DATABASE_PATH':'accounts.db','OPENSTOCKS_DB':'market.db','V2_PAPER_DB':'paper.db',
    'CATALYST_DB':'catalysts.db','BROKER_STATE_DIR':'brokers','BROKER_STATE_PATH':'legacy.json',
    'BROKER_VAULT_KEY_PATH':'private/broker.key','ANGELONE_STATE_DIR':'angelone',
    'OPENSTOCKS_CATALOGUE_DB':'catalogue.db',
}.items():os.environ[_name]=str(_root/_relative)
os.environ['OPENSTOCKS_DISABLE_ENGINE']='1'
os.environ['OPENSTOCKS_DISABLE_V2']='1'
os.environ['TELEGRAM_BOT_TOKEN']=''
os.environ['WHATSAPP_ACCESS_TOKEN']=''

import httpx
_http_transport=httpx.HTTPTransport.handle_request
def _loopback_http(self,request):
    if request.url.host not in {'localhost','127.0.0.1','::1'}:
        raise RuntimeError('Unmocked outbound HTTP is disabled in tests')
    return _http_transport(self,request)
httpx.HTTPTransport.handle_request=_loopback_http
_async_http_transport=httpx.AsyncHTTPTransport.handle_async_request
async def _loopback_async_http(self,request):
    if request.url.host not in {'localhost','127.0.0.1','::1'}:
        raise RuntimeError('Unmocked outbound HTTP is disabled in tests')
    return await _async_http_transport(self,request)
httpx.AsyncHTTPTransport.handle_async_request=_loopback_async_http
