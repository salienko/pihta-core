import asyncio
import hashlib
import json
import os
import secrets
import unittest
from dataclasses import replace
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from multidict import MultiDict
from aiohttp.test_utils import TestClient,TestServer
from settings import load
from booking_store import BookingStore
from booking_domain import TZ,RuleError,estimate
from paykeeper import PayKeeper,verify,acknowledgement,ITEM_NAME,InvoiceNotSent
from paykeeper_store import ACCEPTED,ACCEPTED_READY,RECEIPT_READY
from admin_web import make_app
from bot import deliver_once
from booking_ui import BookingUI

URL=os.getenv('TEST_DATABASE_URL','')


class Protocol(unittest.TestCase):
    def payload(self):
        d=MultiDict(id='123',sum='2000.00',clientid='Гость',orderid='request')
        d['key']=hashlib.md5(('1232000.00Гостьrequest'+'synthetic-secret').encode()).hexdigest()
        return d

    def test_signature_money_and_acknowledgement(self):
        self.assertEqual(verify(self.payload(),'synthetic-secret')[1],200000)
        self.assertEqual(acknowledgement('123','synthetic-secret'),'OK '+hashlib.md5(b'123synthetic-secret').hexdigest())

    def test_tampering_and_duplicate_fields_rejected(self):
        for key in ('id','sum','clientid','orderid','key'):
            d=self.payload();d[key]='different'
            with self.assertRaises(ValueError):verify(d,'synthetic-secret')
        d=self.payload();d.add('sum','2000.00')
        with self.assertRaises(ValueError):verify(d,'synthetic-secret')

    def test_optional_client_fields_and_secret_not_in_repr(self):
        d=MultiDict(id='123',sum='100.00')
        d['key']=hashlib.md5(b'123100.00synthetic-secret').hexdigest()
        self.assertEqual(verify(d,'synthetic-secret')[0]['orderid'],'')
        s=replace(load(),paykeeper_password='do-not-print-password',paykeeper_secret='do-not-print-secret')
        self.assertNotIn('do-not-print',repr(s))

    def test_enabled_requires_explicit_fiscal_configuration(self):
        values=dict(PAYKEEPER_ENABLED='1',PAYKEEPER_URL='https://paykeeper.example',PAYKEEPER_USER='test',PAYKEEPER_PASSWORD='test',PAYKEEPER_SECRET='synthetic-secret-long',WEB_BASE_URL='https://pihta.test')
        with patch.dict(os.environ,values,clear=True):
            with self.assertRaises(ValueError):load()


class Provider(unittest.IsolatedAsyncioTestCase):
    def settings(self):
        return replace(load(),paykeeper_url='https://paykeeper.example',paykeeper_tax='none')

    async def test_invoice_contains_phone_and_explicit_human_receipt_item(self):
        client=PayKeeper(self.settings());client.request=AsyncMock(side_effect=[{'token':'test'},dict(invoice_id='1001',invoice_url='https://evil.example')])
        ident,url=await client.invoice(dict(amount=200000,client_id='Иван',request_id='internal',phone='+79810000000',email='',payment_type='part_prepay'))
        payload=client.request.await_args_list[1].kwargs['data']
        cart=json.loads(payload['service_name'][5:-1])
        self.assertEqual(cart[0]['name'],ITEM_NAME)
        self.assertEqual(cart[0]['sum'],'2000.00')
        self.assertEqual(cart[0]['payment_type'],'part_prepay')
        self.assertEqual(payload['client_phone'],'+79810000000')
        self.assertNotIn('client_email',payload)
        self.assertEqual(url,'https://paykeeper.example/bill/1001/')

    async def test_receipts_match_payment_amount_and_sale(self):
        client=PayKeeper(self.settings())
        base=dict(id='55',payment_id='123',status='success',type='sale',is_post_sale='0',sum_cashless='2000.00',fop_receipt_key='ABCDEFGH')
        client.request=AsyncMock(return_value=dict(receipts=[dict(base,payment_id='456'),dict(base,id='56',sum_cashless='100.00'),dict(base,id='57',status='failed'),dict(base,id='58',type='refund'),base]))
        receipts=await client.receipts(dict(payment_id='123',amount=200000,created_at=datetime.now(TZ)))
        self.assertEqual(receipts,[dict(id='55',url='https://paykeeper.example/receipt/ABCDEFGH/?rc_id=55')])

    async def test_invoice_status_rejects_wrong_order_or_amount(self):
        client=PayKeeper(self.settings());invoice=dict(invoice_id='1',request_id='r',client_id='guest',amount=200000)
        client.request=AsyncMock(return_value=[dict(id='1',orderid='other',clientid='guest',pay_amount='2000.00')])
        with self.assertRaises(Exception):await client.invoice_status(invoice)

    async def test_failed_token_never_sends_invoice_post(self):
        client=PayKeeper(self.settings());client.request=AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(InvoiceNotSent):await client.invoice({})
        self.assertEqual(client.request.await_count,1)


@unittest.skipUnless(URL,'TEST_DATABASE_URL required')
class Payments(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema='test_pk_'+secrets.token_hex(6)
        self.settings=replace(load(),database_url=URL,admin_ids=(1,9),web_url='https://pihta.test',paykeeper_enabled=True,
                              paykeeper_url='https://paykeeper.example',paykeeper_secret='synthetic-secret',paykeeper_tax='none',
                              paykeeper_deposit_type='part_prepay',paykeeper_full_type='prepay',paykeeper_balance_type='part_prepay')
        self.store=await BookingStore.connect(self.settings,self.schema)
        self.provider=SimpleNamespace(invoice=AsyncMock(return_value=('1001','https://paykeeper.example/bill/1001/')),
                                      invoice_status=AsyncMock(return_value={'status':'paid','paymentid':'123'}),receipts=AsyncMock(return_value=[]))
        self.store.provider=lambda:self.provider
        start=(datetime.now(TZ)+timedelta(days=4)).replace(hour=13,minute=0,second=0,microsecond=0)
        token=await self.store.begin(2);d=await self.store.draft(2)
        catalog,holidays=await self.store.pricing()
        data=dict(d['data'],name='Гость',phone='+79810000000',day=start.date().isoformat(),start=start.isoformat(),consent=True,rules_hash=self.settings.rules_hash)
        data['quote']=estimate(data,catalog,holidays)
        await self.store.save(2,d,'review',data)
        self.ident=(await self.store.submit(2,token))[0]
        await self.store.pool.execute('UPDATE outbox SET delivered_at=now()')

    async def asyncTearDown(self):
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE');await self.store.pool.close()

    async def linked(self):
        req,_=await self.store.request_payment(2,self.ident,'deposit')
        await self.store.invoice_once()
        self.req=req
        return req

    async def accept(self,ident='123'):
        return await self.store.accept_paykeeper(dict(id=ident,orderid=self.req['id'],clientid='Гость'),200000)

    async def test_request_duplicate_and_invoice_phone_persist(self):
        first,created=await self.store.request_payment(2,self.ident,'deposit')
        second,again=await self.store.request_payment(2,self.ident,'deposit')
        self.assertTrue(created);self.assertFalse(again);self.assertEqual(first['id'],second['id'])
        await self.store.invoice_once()
        i=self.provider.invoice.await_args.args[0]
        self.assertEqual(i['phone'],'+79810000000')
        self.assertEqual(i['amount'],200000)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM paykeeper_invoices'),1)

    async def test_parallel_webhook_and_poll_credit_once(self):
        await self.linked()
        await asyncio.gather(self.accept(),self.accept(),self.store.poll_once())
        row=await self.store.get(2,self.ident)
        self.assertEqual(row['paid'],200000);self.assertEqual(row['status'],'confirmed')
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM paykeeper_payments'),1)
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM outbox WHERE event='pkpaid:123'"),1)

    async def test_wrong_amount_client_order_and_conflicting_replay(self):
        await self.linked()
        for values,amount in [(dict(id='123',orderid=self.req['id'],clientid='other'),200000),(dict(id='123',orderid=self.req['id'],clientid='Гость'),10000)]:
            with self.assertRaises(ValueError):await self.store.accept_paykeeper(values,amount)
        await self.accept()
        with self.assertRaises(ValueError):await self.store.accept_paykeeper(dict(id='123',orderid=self.req['id'],clientid='Гость'),10000)
        self.assertEqual((await self.store.get(2,self.ident))['paid'],200000)

    async def test_unknown_merchant_payment_is_recorded_without_credit_or_retry_loop(self):
        values=dict(id='999',orderid='external-order',clientid='other')
        await self.store.accept_paykeeper(values,10000)
        await self.store.accept_paykeeper(values,10000)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM paykeeper_unmatched'),1)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM paykeeper_payments'),0)
        self.assertEqual((await self.store.get(2,self.ident))['paid'],0)

    async def test_cancelled_and_second_payment_require_review(self):
        await self.linked();await self.store.transition(1,self.ident,'cancel','Тест отмены',staff=True)
        await self.accept()
        row=await self.store.get(2,self.ident)
        self.assertEqual(row['status'],'cancelled');self.assertEqual(row['paid'],0)
        self.assertEqual(await self.store.pool.fetchval('SELECT disposition FROM paykeeper_payments'),'review')

    async def test_manual_accounting_then_webhook_does_not_double_credit(self):
        await self.linked();await self.store.accounting(1,self.ident,'manual','payment',amount=200000,note='Проверено вручную')
        await self.accept()
        self.assertEqual((await self.store.get(2,self.ident))['paid'],200000)
        self.assertEqual(await self.store.pool.fetchval('SELECT disposition FROM paykeeper_payments'),'review')

    async def test_second_real_payment_is_recorded_but_not_double_applied(self):
        await self.linked();await self.accept();await self.accept('124')
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM paykeeper_payments'),2)
        self.assertEqual((await self.store.get(2,self.ident))['paid'],200000)

    async def test_timeout_never_reissues_post_and_notifies_staff(self):
        self.provider.invoice.side_effect=TimeoutError()
        await self.linked();await self.store.invoice_once()
        self.assertEqual(self.provider.invoice.await_count,1)
        self.assertEqual(await self.store.pool.fetchval('SELECT state FROM paykeeper_invoices'),'uncertain')
        req,created=await self.store.request_payment(2,self.ident,'deposit')
        self.assertFalse(created);self.assertEqual(req['id'],self.req['id'])
        self.assertEqual((await self.store.get(2,self.ident))['status'],'pending')

    async def test_token_failure_is_safely_rescheduled(self):
        self.provider.invoice.side_effect=InvoiceNotSent()
        await self.linked()
        self.assertEqual(await self.store.pool.fetchval('SELECT state FROM paykeeper_invoices'),'queued')
        await self.store.invoice_once()
        self.assertEqual(self.provider.invoice.await_count,1)

    async def test_expired_invoice_keeps_booking_and_allows_new_request(self):
        await self.linked();self.provider.invoice_status.return_value={'status':'expired'}
        await self.store.poll_once()
        self.assertEqual((await self.store.get(2,self.ident))['status'],'pending')
        req,new=await self.store.request_payment(2,self.ident,'deposit')
        self.assertTrue(new);self.assertNotEqual(req['id'],self.req['id'])

    async def test_manual_check_access_and_throttle(self):
        await self.linked();self.provider.invoice_status.return_value={'status':'created'}
        with self.assertRaises(RuleError):await self.store.check_paykeeper(3,self.req['id'])
        self.assertEqual(await self.store.check_paykeeper(2,self.req['id']),'created')
        with self.assertRaises(RuleError):await self.store.check_paykeeper(2,self.req['id'])

    async def test_guest_check_refreshes_booking_card_after_payment(self):
        await self.linked()
        ui=BookingUI(self.settings,self.store);ui.show=AsyncMock()
        message=SimpleNamespace(answer=AsyncMock())
        await ui._callback(SimpleNamespace(from_user=SimpleNamespace(id=2),message=message,data='paycheck:'+self.req['id']))
        ui.show.assert_awaited_once_with(message,2,self.ident,edit=True)
        self.assertEqual((await self.store.get(2,self.ident))['status'],'confirmed')
        from booking_domain import card
        self.assertIn('Бронь подтверждена, требуется доплата',card(await self.store.get(2,self.ident)))

    async def test_deposit_link_replaces_request_with_amount_check(self):
        await self.linked()
        ui=BookingUI(self.settings,self.store);ui.say=AsyncMock()
        await ui.show(SimpleNamespace(),2,self.ident)
        rows=ui.say.await_args.args[2]
        self.assertEqual(rows[0][0][0],'Проверить оплату 2 000 ₽')
        self.assertEqual(rows[0][0][1],'paycheck:'+self.req['id'])
        self.assertFalse(any('Внести предоплату' in label for row in rows for label,_ in row))
        self.provider.invoice_status.return_value={'status':'created'}
        await self.store.check_paykeeper(2,self.req['id'])
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM outbox WHERE event=$1','pkguestcheck:'+self.req['id']),2)
        self.assertEqual((await self.store.get(2,self.ident))['paid'],0)

    async def test_full_payment_link_replaces_full_payment_button(self):
        req,_=await self.store.request_payment(2,self.ident,'full')
        await self.store.invoice_once()
        ui=BookingUI(self.settings,self.store);ui.say=AsyncMock()
        await ui.show(SimpleNamespace(),2,self.ident)
        rows=ui.say.await_args.args[2]
        self.assertEqual(rows[1][0][1],'paycheck:'+req['id'])
        self.assertTrue(rows[1][0][0].startswith('Проверить оплату '))
        self.assertFalse(any(action=='payrequest:full:'+self.ident for row in rows for _,action in row))

    async def test_balance_link_replaces_balance_payment_button(self):
        await self.linked();await self.accept()
        req,_=await self.store.request_payment(2,self.ident,'balance')
        self.provider.invoice.return_value=('1002','https://paykeeper.example/bill/1002/')
        await self.store.invoice_once()
        ui=BookingUI(self.settings,self.store);ui.say=AsyncMock()
        await ui.show(SimpleNamespace(),2,self.ident)
        rows=ui.say.await_args.args[2]
        self.assertEqual(rows[0][0][1],'paycheck:'+req['id'])
        self.assertTrue(rows[0][0][0].startswith('Проверить оплату '))
        self.assertFalse(any(action=='payrequest:balance:'+self.ident for row in rows for _,action in row))

    async def test_receipt_merges_with_unsent_payment_notice_and_persists(self):
        await self.linked();await self.accept()
        self.provider.receipts.return_value=[dict(id='55',url='https://paykeeper.example/receipt/ABCDEFGH/?rc_id=55')]
        await self.store.receipt_once()
        from booking_domain import payment_confirmation
        self.assertEqual(await self.store.pool.fetchval("SELECT text FROM outbox WHERE event='pkpaid:123'"),payment_confirmation(await self.store.get(2,self.ident))+' '+ACCEPTED_READY)
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM outbox WHERE event='pkreceipt:55'"),0)
        history=await self.store.payment_history(2,self.ident)
        self.assertIn('ABCDEFGH',history[0]['receipt_url'])
        with self.assertRaises(RuleError):await self.store.payment_history(3,self.ident)
        other=await BookingStore.connect(self.settings,self.schema)
        try:self.assertEqual((await other.payment_history(2,self.ident))[0]['receipt_id'],'55')
        finally:await other.pool.close()

    async def test_delayed_receipt_sends_explained_separate_message_once(self):
        await self.linked();await self.accept()
        await self.store.pool.execute("UPDATE outbox SET delivered_at=now() WHERE event='pkpaid:123'")
        self.provider.receipts.return_value=[dict(id='55',url='https://paykeeper.example/receipt/ABCDEFGH/?rc_id=55')]
        await self.store.receipt_once();await self.store.receipt_once()
        self.assertEqual(await self.store.pool.fetchval("SELECT text FROM outbox WHERE event='pkreceipt:55'"),RECEIPT_READY)
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM outbox WHERE event='pkreceipt:55'"),1)

    async def test_missing_receipt_retries_without_recrediting(self):
        await self.linked();await self.accept();await self.store.receipt_once()
        self.assertFalse(await self.store.pool.fetchval('SELECT receipt_done FROM paykeeper_payments'))
        self.assertEqual((await self.store.get(2,self.ident))['paid'],200000)

    async def test_delayed_webhook_uses_invoice_date_for_receipt_search(self):
        await self.linked()
        await self.store.pool.execute("UPDATE paykeeper_invoices SET created_at=now()-interval '2 days'")
        await self.accept();await self.store.receipt_once()
        payment=self.provider.receipts.await_args.args[0]
        self.assertLess(payment['created_at'],datetime.now(TZ)-timedelta(days=1))

    async def test_standalone_staff_invoice_and_receipt_only_notify_staff(self):
        key=secrets.token_hex(16)
        i=await self.store.create_staff_invoice(1,key,'+79810000000',10000,'full')
        again=await self.store.create_staff_invoice(1,key,'+79810000000',10000,'full')
        self.assertEqual(i['request_id'],again['request_id'])
        with self.assertRaises(RuleError):await self.store.create_staff_invoice(1,key,'+79810000000',20000,'full')
        with self.assertRaises(RuleError):await self.store.create_staff_invoice(2,secrets.token_hex(16),'+79810000000',10000,'full')
        await self.store.invoice_once()
        await self.store.accept_paykeeper(dict(id='123',orderid=i['request_id'],clientid='+79810000000'),10000)
        self.assertEqual(await self.store.pool.fetchval('SELECT disposition FROM paykeeper_payments'),'standalone')
        self.assertEqual(set(await self.store.pool.fetchval('SELECT array_agg(DISTINCT recipient) FROM outbox WHERE booking_id IS NULL')),{1,9})

    async def test_custom_partial_payment_threshold(self):
        i=await self.store.create_staff_invoice(1,secrets.token_hex(16),'+79810000000',50000,'part_prepay',booking_id=self.ident)
        await self.store.accept_paykeeper(dict(id='123',orderid=i['request_id'],clientid='Гость'),50000)
        self.assertEqual((await self.store.get(2,self.ident))['status'],'pending')
        i=await self.store.create_staff_invoice(1,secrets.token_hex(16),'+79810000000',150000,'part_prepay',booking_id=self.ident)
        await self.store.accept_paykeeper(dict(id='124',orderid=i['request_id'],clientid='Гость'),150000)
        self.assertEqual((await self.store.get(2,self.ident))['status'],'confirmed')

    async def test_webhook_commits_before_ack_and_rejects_bad_signature(self):
        await self.linked()
        app=make_app(self.store,self.settings,secure=False)
        data=dict(id='123',sum='2000.00',clientid='Гость',orderid=self.req['id'])
        data['key']=hashlib.md5(('1232000.00Гость'+self.req['id']+self.settings.paykeeper_secret).encode()).hexdigest()
        async with TestClient(TestServer(app)) as client:
            response=await client.post('/payments/paykeeper/notify',data=data)
            self.assertEqual(response.status,200);self.assertEqual(await response.text(),acknowledgement('123',self.settings.paykeeper_secret))
            self.assertEqual((await self.store.get(2,self.ident))['paid'],200000)
            response=await client.post('/payments/paykeeper/notify',data=dict(data,sum='3000.00'))
            self.assertEqual(response.status,403)

    async def test_admin_payment_page_requires_session_and_csrf(self):
        async with TestClient(TestServer(make_app(self.store,self.settings,secure=False))) as client:
            response=await client.get('/payments',allow_redirects=False)
            self.assertEqual(response.status,302)
            token=await self.store.login(await self.store.login_token(1))
            client.session.cookie_jar.update_cookies({'pihta_session':token})
            response=await client.get('/payments')
            self.assertEqual(response.status,200)
            self.assertIn('Выставить счёт',await response.text())
            response=await client.post('/payments',data={'phone':'+79810000000','amount':'100','operation':secrets.token_hex(16)},allow_redirects=False)
            self.assertEqual(response.status,403)

    async def test_booking_invoice_form_prefills_guest_and_due(self):
        async with TestClient(TestServer(make_app(self.store,self.settings,secure=False))) as client:
            token=await self.store.login(await self.store.login_token(1))
            client.session.cookie_jar.update_cookies({'pihta_session':token})
            response=await client.get('/payments?booking='+self.ident)
            body=await response.text()
            self.assertEqual(response.status,200)
            self.assertIn('Имя гостя',body);self.assertIn('value="Гость"',body)
            self.assertIn('value="+79810000000" readonly',body)
            row=await self.store.get(2,self.ident)
            self.assertIn('value="'+f'{row["estimate"]["total"]//100}.00'+'" required',body)

    async def test_receipt_button_delivery_does_not_use_someone_elses_chat(self):
        await self.linked();await self.accept()
        self.provider.receipts.return_value=[dict(id='55',url='https://paykeeper.example/receipt/ABCDEFGH/?rc_id=55')]
        await self.store.receipt_once()
        await self.store.pool.execute("UPDATE outbox SET delivered_at=now() WHERE event!='pkpaid:123'")
        bot=SimpleNamespace(send_message=AsyncMock())
        self.assertTrue(await deliver_once(bot,self.store))
        args=bot.send_message.await_args
        self.assertEqual(args.args[0],2)
        self.assertEqual(args.kwargs['reply_markup'].inline_keyboard[0][0].text,'Открыть чек')
