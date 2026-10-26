"""PayKeeper protocol. Credentials and provider responses must never be logged."""
import hashlib
import hmac
import re
import json
from decimal import Decimal
from booking_domain import TZ
from aiohttp import BasicAuth, ClientSession, ClientTimeout, web

ITEM_NAME = 'Оплата аренды бани «Пихта»'


class ProviderError(Exception):
    """A safe error: never includes response bodies or credentials."""


class InvoiceNotSent(ProviderError):
    """Failure before the mutating invoice request; safe to retry."""


def objects(data):
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict) and isinstance(data.get('receipts'), list):
        return [r for r in data['receipts'] if isinstance(r, dict)]
    return [data] if isinstance(data, dict) and 'id' in data else []

def kopecks(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,9}(?:\.[0-9]{1,2})?', value):
        raise ValueError('Invalid amount')
    amount = int(Decimal(value) * 100)
    if amount <= 0:
        raise ValueError('Invalid amount')
    return amount


def verify(data, secret):
    fields = ('id', 'sum', 'clientid', 'orderid', 'key')
    if not secret or any(len(data.getall(k, [])) != 1 for k in ('id','sum','key')) or any(len(data.getall(k, []))>1 for k in ('clientid','orderid')):
        raise ValueError('Invalid fields')
    values = {k: data.get(k,'') for k in fields}
    if any(not isinstance(v, str) or len(v) > 200 for v in values.values()):
        raise ValueError('Invalid fields')
    if not re.fullmatch(r'[0-9]{1,40}', values['id']):
        raise ValueError('Invalid payment ID')
    amount = kopecks(values['sum'])
    digest = hashlib.md5((''.join(values[k] for k in fields[:4]) + secret).encode()).hexdigest()
    if not hmac.compare_digest(digest, values['key']):
        raise ValueError('Invalid signature')
    return values, amount


def acknowledgement(ident, secret):
    return 'OK ' + hashlib.md5((ident + secret).encode()).hexdigest()


class PayKeeper:
    def __init__(self, settings):
        self.settings = settings

    async def request(self, path, params=None, data=None):
        s=self.settings
        async with ClientSession(auth=BasicAuth(s.paykeeper_user,s.paykeeper_password),timeout=ClientTimeout(total=20)) as session:
            async with session.request('POST' if data is not None else 'GET',s.paykeeper_url+path,
                                       params=params,data=data,allow_redirects=False) as response:
                if response.status!=200:raise ProviderError('provider_http_error')
                raw=bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw)>1048576:raise ProviderError('provider_response_too_large')
        try:result=json.loads(raw)
        except (ValueError,UnicodeError):raise ProviderError('provider_invalid_json') from None
        if isinstance(result,dict) and (result.get('error') or result.get('result') in ('error','fail')):
            raise ProviderError('provider_rejected_request')
        return result

    async def invoice(self, row):
        s=self.settings
        try:
            token=(await self.request('/info/settings/token/'))['token']
            if not isinstance(token,str) or not token:raise ProviderError('provider_missing_token')
        except Exception:raise InvoiceNotSent('token_request_failed') from None
        amount=f'{row["amount"]//100}.{row["amount"]%100:02}'
        cart=[dict(name=ITEM_NAME,price=amount,quantity=1,sum=amount,tax=s.paykeeper_tax,
                   item_type='service',payment_type=row['payment_type'])]
        payload=dict(token=token,pay_amount=amount,clientid=row['client_id'],orderid=row['request_id'],
                     client_phone=row['phone'],service_name=';PKC|'+json.dumps(cart,ensure_ascii=False)+'|')
        if row.get('email'):payload['client_email']=row['email']
        result=await self.request('/change/invoice/preview/',data=payload)
        ident=str(result.get('invoice_id',''))
        if not re.fullmatch(r'[0-9]{1,40}',ident):raise ProviderError('provider_missing_invoice')
        # Use only the configured trusted origin, never a URL from a provider body.
        return ident,s.paykeeper_url+'/bill/'+ident+'/'

    async def invoice_status(self,invoice):
        data=await self.request('/info/invoice/byid/',params={'id':invoice['invoice_id']})
        row=next((r for r in objects(data) if str(r.get('id'))==invoice['invoice_id']),None)
        if not row or row.get('orderid')!=invoice['request_id'] or str(row.get('clientid'))!=invoice['client_id'] or kopecks(str(row.get('pay_amount','')))!=invoice['amount']:
            raise ProviderError('provider_invoice_mismatch')
        return row

    async def create_settlement(self,row):
        token=(await self.request('/info/settings/token/'))['token']
        amount=f'{row["amount"]//100}.{row["amount"]%100:02}'
        cart=[dict(name=ITEM_NAME,quantity=1,price=amount,sum=amount,item_type='service',payment_type='full',tax=row['tax'])]
        result=await self.request('/change/receipt/print/',data=dict(token=token,payment_id='',refund_id='',type='sale',is_post_sale='true',is_correction='false',contact=row['phone'],sum_cashless='0.00',sum_cash='0.00',sum_advance=amount,sum_credit='0.00',sum_counter_payment='0.00',cart=json.dumps(cart,ensure_ascii=False),receipt_key=row['receipt_key']))
        ident=str(result.get('receipt_id',''))
        if not re.fullmatch(r'[0-9]{1,40}',ident):raise ProviderError('provider_missing_receipt')
        return ident

    async def settlement_status(self,row):
        ident=str(row['receipt_id'])
        if not re.fullmatch(r'[0-9]{1,40}',ident):raise ProviderError('invalid_receipt_id')
        data=await self.request('/info/receipts/byid/',params={'id':ident})
        if not isinstance(data,dict):raise ProviderError('receipt_mismatch')
        try:
            cart=data['cart'];cart=json.loads(cart) if isinstance(cart,str) else cart
            valid=(str(data['id'])==ident and data['fop_receipt_key']==row['receipt_key'] and data['type']=='sale'
                   and str(data['is_post_sale'])=='1' and str(data.get('is_correction','0'))=='0'
                   and str(data.get('payment_id') or '') in ('','0') and data['contact']==row['phone']
                   and Decimal(str(data['sum_advance']))*100==row['amount']
                   and all(Decimal(str(data.get(k,'0')))==0 for k in ('sum_cashless','sum_cash','sum_credit','sum_counter_payment'))
                   and len(cart)==1 and cart[0]['name']==ITEM_NAME and cart[0]['payment_type']=='full'
                   and cart[0]['tax']==row['tax'] and cart[0]['item_type']=='service'
                   and Decimal(str(cart[0]['sum']))*100==row['amount'] and Decimal(str(cart[0]['quantity']))==1)
        except (KeyError,ValueError,TypeError,ArithmeticError):valid=False
        if not valid:raise ProviderError('receipt_mismatch')
        status=data.get('status')
        if status=='success':return dict(state='success',url=self.settings.paykeeper_url+'/receipt/'+row['receipt_key']+'/?rc_id='+ident)
        if status in ('failed','rejected'):return dict(state='failed')
        # timeout can acquire fiscal attributes later; never reissue automatically.
        if status in ('created','sending','request_sent','timeout'):return dict(state='pending')
        raise ProviderError('receipt_unknown_status')

    async def receipts(self,payment):
        start=payment['created_at'].astimezone(TZ).date().isoformat()
        from datetime import datetime
        data=await self.request('/info/receipts/search/',params=dict(query=payment['payment_id'],start=start,end=datetime.now(TZ).date().isoformat()))
        result=[]
        for row in objects(data):
            if str(row.get('payment_id'))!=payment['payment_id'] or row.get('status')!='success' or row.get('type')!='sale' or str(row.get('is_post_sale','0'))!='0':continue
            ident,key=str(row.get('id','')),row.get('fop_receipt_key','')
            if not re.fullmatch(r'[0-9]{1,40}',ident) or not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{6,128}',key):continue
            try:amount=kopecks(str(row.get('sum_cashless','')))
            except ValueError:continue
            if amount!=payment['amount']:continue
            result.append(dict(id=ident,url=self.settings.paykeeper_url+'/receipt/'+key+'/?rc_id='+ident))
        return result


def mount(app, store, settings):
    async def notification(request):
        if not settings.paykeeper_enabled:
            raise web.HTTPServiceUnavailable(text='Disabled')
        if request.content_type != 'application/x-www-form-urlencoded':
            raise web.HTTPUnsupportedMediaType()
        try:
            values, amount = verify(await request.post(), settings.paykeeper_secret)
        except (ValueError, TypeError, KeyError):
            raise web.HTTPForbidden(text='Invalid notification') from None
        try:
            await store.accept_paykeeper(values, amount)
        except ValueError:
            raise web.HTTPConflict(text='Payment mismatch') from None
        # Return acknowledgement only after the accounting transaction commits.
        return web.Response(text=acknowledgement(values['id'], settings.paykeeper_secret))
    app.router.add_post('/payments/paykeeper/notify', notification)
