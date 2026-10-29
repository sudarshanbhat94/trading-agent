"""Owner-bound Angel One cash adapter. Transport is not route certification.

No login/password/TOTP generation is automated here. The owner completes
broker authentication separately. Production entries still require an exact
reviewed release scope and orchestration; native protection is unsupported.
Official contract: https://smartapi.angelone.in/docs
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .credential_vault import unseal
from .instrument_catalog import InstrumentError
from .live_release import authorized

BASE='https://apiconnect.angelone.in'
ROOT='/rest/secure/angelbroking/'
PRODUCT={'D':'DELIVERY','I':'INTRADAY'}


def private_credentials(uid):
    if type(uid) is not int or uid<1:raise InstrumentError('Owned Angel One account required')
    directory=Path(os.environ.get('ANGELONE_STATE_DIR',Path(__file__).resolve().parents[1]/'var/brokers/angelone'))
    path=directory/(str(uid)+'.json')
    if path.is_symlink() or not path.is_file() or path.stat().st_mode&0o077:
        raise InstrumentError('Private account-bound Angel One credentials unavailable')
    state=unseal(directory,uid,json.loads(path.read_text()))
    if type(state.get('owner_user_id')) is not int or state['owner_user_id']!=uid:raise InstrumentError('Angel One credential owner mismatch')
    return state


class AngelOnePort:
    name='angelone'

    def __init__(self,credentials=private_credentials,request=None):
        self.credentials=credentials
        if request is None:
            import httpx
            request=httpx.request
        self.request=request

    def _request(self,uid,method,path,payload=None):
        if type(uid) is not int or uid<1:raise InstrumentError('Owned broker account required')
        state=self.credentials(uid)
        if type(state.get('owner_user_id')) is not int or state['owner_user_id']!=uid:raise InstrumentError('Angel One owner mismatch')
        fields=('access_token','api_key','client_local_ip','client_public_ip','client_mac')
        if any(not isinstance(state.get(k),str) or not state[k].strip() for k in fields):
            raise InstrumentError('Angel One authenticated session metadata unavailable')
        try:
            expires=datetime.fromisoformat(state['expires_at'].replace('Z','+00:00'))
            if expires.tzinfo is None or expires<=datetime.now(ZoneInfo('UTC')):raise ValueError()
        except (ValueError,KeyError,TypeError):raise InstrumentError('Angel One session expired or unknown')
        headers={'Authorization':'Bearer '+state['access_token'],'X-PrivateKey':state['api_key'],
                 'X-ClientLocalIP':state['client_local_ip'],'X-ClientPublicIP':state['client_public_ip'],
                 'X-MACAddress':state['client_mac'],'X-UserType':'USER','X-SourceID':'WEB',
                 'Accept':'application/json','Content-Type':'application/json'}
        # Exactly one transmission. Timeout ambiguity belongs to the journal.
        response=self.request(method,BASE+ROOT+path,headers=headers,json=payload,timeout=15,follow_redirects=False)
        response.raise_for_status();body=response.json()
        if not isinstance(body,dict) or body.get('status') is not True:
            raise InstrumentError('Angel One request refused or ambiguous; reconcile before retry')
        return body.get('data')

    def _rows(self,uid,path):
        data=self._request(uid,'GET',path)
        if not isinstance(data,list) or any(not isinstance(row,dict) for row in data):raise InstrumentError('Complete Angel One evidence unavailable')
        return data

    @staticmethod
    def _key(row):
        exchange=row.get('exchange');token=row.get('symboltoken')
        if exchange!='NSE' or not isinstance(token,str) or not token:
            raise InstrumentError('Unsupported or ambiguous Angel One contract')
        return 'NSE_EQ|'+token

    @staticmethod
    def _number(value, *, positive=False):
        try:
            if isinstance(value,bool):raise ValueError()
            number=Decimal(str(value))
            if not number.is_finite() or number<0 or (positive and number<=0):raise ValueError()
            return number
        except (InvalidOperation,ValueError,TypeError):raise InstrumentError('Malformed Angel One numeric evidence')

    def submit(self,uid,instrument_key,quantity,side,*,product,tag,symbol=None):
        allowed,reason=authorized(uid,broker='angelone',product=product,model='manual')
        if not allowed:raise InstrumentError(reason)
        if not isinstance(instrument_key,str) or not instrument_key.startswith('NSE_EQ|') or not instrument_key.split('|',1)[1] or \
                type(quantity) is not int or quantity<1 or side not in {'BUY','SELL'} or product not in PRODUCT or \
                not isinstance(symbol,str) or not symbol.endswith('-EQ') or not isinstance(tag,str) or not 1<=len(tag)<20:
            raise InstrumentError('Sourced NSE cash symbol/token and stable intent tag required')
        payload=dict(variety='NORMAL',tradingsymbol=symbol,symboltoken=instrument_key.split('|',1)[1],
                     transactiontype=side,exchange='NSE',ordertype='MARKET',producttype=PRODUCT[product],
                     duration='DAY',price='0',squareoff='0',stoploss='0',quantity=str(quantity),ordertag=tag)
        data=self._request(uid,'POST','order/v1/placeOrder',payload)
        if not isinstance(data,dict) or not data.get('orderid'):return {'ok':False,'status':'unknown'}
        return dict(ok=True,order_id=str(data['orderid']),unique_order_id=data.get('uniqueorderid'))

    def orders(self,uid):
        rows=[]
        for raw in self._rows(uid,'order/v1/getOrderBook'):
            product=next((k for k,v in PRODUCT.items() if v==raw.get('producttype')),None)
            if product is None:raise InstrumentError('Unsupported Angel One account product')
            qty=self._number(raw.get('filledshares'))
            if qty<0 or qty!=int(qty):raise InstrumentError('Malformed filled quantity')
            if raw.get('transactiontype') not in {'BUY','SELL'} or not raw.get('orderid'):
                raise InstrumentError('Malformed Angel One order identity')
            rows.append(dict(order_id=str(raw['orderid']),tag=raw.get('ordertag'),instrument_token=self._key(raw),
                product=product,transaction_type=raw['transactiontype'],filled_quantity=int(qty),
                average_price=float(self._number(raw.get('averageprice'),positive=bool(qty))),status=raw.get('orderstatus') or raw.get('status')))
        return rows

    def order_status(self,uid,order_id):
        rows=[r for r in self.orders(uid) if r['order_id']==order_id]
        if len(rows)>1:raise InstrumentError('Ambiguous Angel One order evidence')
        return rows[0] if rows else None

    def trades(self,uid):
        rows=[];today=datetime.now(ZoneInfo('Asia/Kolkata')).date()
        for raw in self._rows(uid,'order/v1/getTradeBook'):
            # Documented current-day tradebook; an undated archived payload
            # cannot be ingested through this adapter as historical evidence.
            try:executed=datetime.combine(today,datetime.strptime(raw['filltime'],'%H:%M:%S').time(),ZoneInfo('Asia/Kolkata'))
            except (ValueError,KeyError,TypeError):raise InstrumentError('Angel One trade execution time unavailable')
            qty=self._number(raw.get('fillsize'),positive=True)
            if qty<=0 or qty!=int(qty):raise InstrumentError('Malformed Angel One trade size')
            product=next((k for k,v in PRODUCT.items() if v==raw.get('producttype')),None)
            if product is None:raise InstrumentError('Unsupported Angel One trade product')
            if raw.get('transactiontype') not in {'BUY','SELL'} or not raw.get('orderid') or not raw.get('fillid'):
                raise InstrumentError('Malformed Angel One trade identity')
            rows.append(dict(order_id=str(raw['orderid']),trade_id=str(raw['fillid']),instrument_token=self._key(raw),
                product=product,transaction_type=raw['transactiontype'],quantity=int(qty),average_price=float(self._number(raw.get('fillprice'),positive=True)),executed_at=executed.isoformat()))
        return rows

    @staticmethod
    def _quantity(value):
        if isinstance(value,bool):raise InstrumentError('Malformed inventory quantity')
        try:qty=Decimal(str(value))
        except (InvalidOperation,ValueError,TypeError):raise InstrumentError('Malformed inventory quantity')
        if not qty.is_finite() or qty!=int(qty):raise InstrumentError('Malformed inventory quantity')
        return int(qty)

    def positions(self,uid):
        rows=[]
        for raw in self._rows(uid,'order/v1/getPosition'):
            product=next((k for k,v in PRODUCT.items() if v==raw.get('producttype')),None)
            if product is None:raise InstrumentError('Unsupported Angel One inventory product')
            overnight=self._quantity(raw['cfbuyqty'])-self._quantity(raw['cfsellqty'])
            rows.append(dict(instrument_token=self._key(raw),product=product,quantity=self._quantity(raw['netqty']),
                             overnight_quantity=overnight))
        return rows
    def holdings(self,uid):
        return [dict(instrument_token=self._key(r),quantity=self._quantity(r['quantity']),
                     t1_quantity=self._quantity(r['t1quantity'])) for r in self._rows(uid,'portfolio/v1/getHolding')]
    def funds(self,uid):
        raw=self._request(uid,'GET','user/v1/getRMS')
        # Cash only; collateral/net leverage is not spendable sleeve capital.
        if not isinstance(raw,dict):raise InstrumentError('Angel One free cash unavailable')
        cash=self._number(raw.get('availablecash'))
        return {'data':{'equity':{'available_margin':float(cash)}},'source':'Angel One availablecash; collateral excluded'}
    def margin(self,uid,instruments):
        if not isinstance(instruments,list) or not 1<=len(instruments)<=50:raise InstrumentError('Dated margin request required')
        for row in instruments:
            if not isinstance(row,dict) or row.get('exchange')!='NSE' or type(row.get('qty')) is not int or row['qty']<1 or \
                    row.get('productType') not in PRODUCT.values() or row.get('tradeType') not in {'BUY','SELL'} or \
                    row.get('orderType')!='MARKET' or not row.get('token') or row.get('price')!=0:
                raise InstrumentError('Unsupported Angel One margin route')
        return self._request(uid,'POST','margin/v1/batch',{'positions':instruments})
    def cancel(self,uid,order_id):
        if not isinstance(order_id,str) or not order_id:raise InstrumentError('Owned order identity required')
        return self._request(uid,'POST','order/v1/cancelOrder',{'variety':'NORMAL','orderid':order_id})
    def modify(self,uid,order_id,*,quantity,original=None):
        if type(quantity) is not int or quantity<1 or not isinstance(original,dict) or \
                original.get('orderid')!=order_id or original.get('variety')!='NORMAL' or original.get('exchange')!='NSE' or \
                original.get('ordertype')!='MARKET' or original.get('producttype') not in PRODUCT.values() or \
                original.get('duration')!='DAY' or not original.get('symboltoken') or not original.get('tradingsymbol'):
            raise InstrumentError('Full approved original cash intent required for Angel One modification')
        payload={k:original[k] for k in ('variety','orderid','ordertype','producttype','duration','tradingsymbol','symboltoken','exchange')}
        payload.update(price='0',quantity=str(quantity))
        return self._request(uid,'POST','order/v1/modifyOrder',payload)
    def place_stop(self,*args,**kwargs):raise InstrumentError('Angel One native protection is not certified')
    def protection_status(self,*args,**kwargs):raise InstrumentError('Angel One native protection is not certified')
    def cancel_protection(self,*args,**kwargs):raise InstrumentError('Angel One native protection is not certified')


def capabilities():
    return dict(broker='angelone',transport_implemented=True,orchestration_enabled=False,live_certified=False,
                scope='NSE cash session transport',native_protection=False,
                unsupported=['native protection','partial replacement','derivatives','BSE','US'],
                note='Isolated adapter tests are not broker certification or automatic account activation')
