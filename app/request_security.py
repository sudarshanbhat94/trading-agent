"""Browser mutation boundary. Proxy metadata is trusted by ASGI, not headers."""
import os
from urllib.parse import urlsplit
from starlette.responses import JSONResponse

UNSAFE={'POST','PUT','PATCH','DELETE'}


def _origin(value):
    try:
        parsed=urlsplit(value)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or \
                parsed.path not in {'','/'} or parsed.query or parsed.fragment:return None
        port=parsed.port or (443 if parsed.scheme=='https' else 80)
        return parsed.scheme,parsed.hostname.lower(),port
    except (ValueError,TypeError):return None


def refusal(request):
    if request.method not in UNSAFE:return None
    if request.headers.get('sec-fetch-site')=='cross-site':return 'Cross-site account mutation refused'
    supplied=request.headers.get('origin')
    expected=os.environ.get('OPENSTOCKS_PUBLIC_ORIGIN') or str(request.base_url)
    if supplied is not None and (_origin(supplied) is None or _origin(supplied)!=_origin(expected)):
        return 'Request origin does not match this application'
    from .auth import SESSION_COOKIE
    if request.cookies.get(SESSION_COOKIE) and supplied is None:
        return 'Cookie-authenticated mutations require a matching Origin header'
    return None


async def boundary(request,call_next):
    reason=refusal(request)
    if reason:return JSONResponse({'error':reason,'code':'CSRF_ORIGIN_REFUSAL'},status_code=403)
    response=await call_next(request)
    response.headers.setdefault('X-Content-Type-Options','nosniff')
    response.headers.setdefault('Referrer-Policy','same-origin')
    response.headers.setdefault('X-Frame-Options','DENY')
    return response
