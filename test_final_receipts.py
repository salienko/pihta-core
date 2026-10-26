import asyncio
import json
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock
import test_paykeeper as fixtures
from booking_domain import RuleError
from paykeeper import PayKeeper,ITEM_NAME,ProviderError
from aiohttp.test_utils import TestClient,TestServer
from admin_web import make_app


class Protocol(unittest.IsolatedAsyncioTestCase):
    async def test_zero_new_money_and_full_advance(self):
        client=PayKeeper(fixtures.Provider().settings())
        client.request=AsyncMock(side_effect=[{'token':'synthetic'},{'receipt_id':42}])
        self.assertEqual(await client.create_settlement(dict(amount=650000,phone='+79000000000',tax='none',receipt_key='synthetic-key')), '42')
        data=client.request.await_args.kwargs['data']
        self.assertEqual(data['sum_advance'],'6500.00');self.assertEqual(data['sum_cashless'],'0.00')
        self.assertEqual(data['payment_id'],'');self.assertEqual(data['is_post_sale'],'true')
        self.assertEqual(json.loads(data['cart'])[0]['payment_type'],'full')

    async def test_result_validates_key_amount_and_cart(self):
        client=PayKeeper(fixtures.Provider().settings())
        row=dict(receipt_id='42',receipt_key='synthetic-key',phone='+79000000000',amount=650000,tax='none')
        response=dict(id='42',fop_receipt_key='synthetic-key',type='sale',is_post_sale='1',contact=row['phone'],sum_advance='6500.00',status='success',cart=[dict(name=ITEM_NAME,payment_type='full',tax='none',item_type='service',sum='6500.00',quantity=1)])
        client.request=AsyncMock(return_value=response)
        self.assertEqual((await client.settlement_status(row))['state'],'success')
        for change in ({'fop_receipt_key':'wrong-key'},{'sum_advance':'1.00'},{'sum_cashless':'6500.00'},{'is_post_sale':'0'}):
            client.request=AsyncMock(return_value=dict(response,**change))
            with self.assertRaises(ProviderError):await client.settlement_status(row)
        client.request=AsyncMock(return_value=dict(response,status='timeout'))
        self.assertEqual((await client.settlement_status(row))['state'],'pending')


@unittest.skipUnless(fixtures.URL,'TEST_DATABASE_URL required')
class Settlements(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from dataclasses import replace
        await fixtures.Payments.asyncSetUp(self)
        self.settings=replace(self.settings,paykeeper_settlement_enabled=True)
        self.store.settings=self.settings
    asyncTearDown=fixtures.Payments.asyncTearDown

    async def ready(self):
        req,_=await self.store.request_payment(2,self.ident,'full')
        await self.store.invoice_once()
        await self.store.accept_paykeeper(dict(id='123',orderid=req['id'],clientid='Гость'),req['amount'])
        await self.store.pool.execute("INSERT INTO paykeeper_fiscal_receipts VALUES('99','123','https://paykeeper.example/receipt/synthetic',now())")
        row=await self.store.get(2,self.ident)
        self.store.clock=AsyncMock(return_value=row['ends_at']+timedelta(seconds=1))
        self.provider.create_settlement=AsyncMock(return_value='555')
        self.provider.settlement_status=AsyncMock(return_value=dict(state='success',url='https://paykeeper.example/receipt/final'))

    async def test_concurrent_clicks_and_workers_issue_once_and_deliver_once(self):
        await self.ready()
        a,b=await asyncio.gather(self.store.request_final_receipt(1,self.ident),self.store.request_final_receipt(9,self.ident))
        self.assertEqual(a['receipt_key'],b['receipt_key'])
        await asyncio.gather(self.store.settlement_once(),self.store.settlement_once())
        self.assertEqual(self.provider.create_settlement.await_count,1)
        await self.store.pool.execute('UPDATE paykeeper_settlements SET next_check_at=now()')
        await self.store.settlement_once();await self.store.settlement_once()
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM outbox WHERE event=$1",'pkfinal:'+self.ident),1)
        self.assertEqual(await self.store.receipt_delivery_url('pkfinal:'+self.ident),'https://paykeeper.example/receipt/final')

    async def test_unknown_response_never_reissues(self):
        await self.ready();await self.store.request_final_receipt(1,self.ident)
        self.provider.create_settlement.side_effect=TimeoutError()
        await self.store.settlement_once();await self.store.settlement_once()
        self.assertEqual(self.provider.create_settlement.await_count,1)
        self.assertEqual(await self.store.pool.fetchval('SELECT state FROM paykeeper_settlements'),'uncertain')

    async def test_reject_unpaid_nonstaff_early_and_no_show(self):
        with self.assertRaises(RuleError):await self.store.request_final_receipt(2,self.ident)
        with self.assertRaises(RuleError):await self.store.request_final_receipt(1,self.ident)
        await self.ready();self.store.clock.reset_mock()
        row=await self.store.get(2,self.ident)
        self.store.clock.return_value=row['starts_at']
        with self.assertRaises(RuleError):await self.store.request_final_receipt(1,self.ident)
        self.store.clock.return_value=row['ends_at']+timedelta(seconds=1)
        await self.store.pool.execute('UPDATE bookings SET no_show=true WHERE id=$1',self.ident)
        with self.assertRaises(RuleError):await self.store.request_final_receipt(1,self.ident)

    async def test_reject_full_calculation_and_freeze_accounting(self):
        await self.ready()
        await self.store.pool.execute("UPDATE paykeeper_invoices SET payment_type='full'")
        with self.assertRaises(RuleError):await self.store.request_final_receipt(1,self.ident)
        await self.store.pool.execute("UPDATE paykeeper_invoices SET payment_type='prepay'")
        await self.store.request_final_receipt(1,self.ident)
        with self.assertRaises(RuleError):await self.store.accounting(1,self.ident,'key','refund',amount=100,note='test refund')

    async def test_failed_receipt_never_sends_success(self):
        await self.ready();await self.store.request_final_receipt(1,self.ident);await self.store.settlement_once()
        self.provider.settlement_status.return_value={'state':'failed'}
        await self.store.pool.execute('UPDATE paykeeper_settlements SET next_check_at=now()')
        await self.store.settlement_once()
        self.assertEqual(await self.store.pool.fetchval('SELECT state FROM paykeeper_settlements'),'failed')
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM outbox WHERE event=$1','pkfinal:'+self.ident),0)

    async def test_two_partial_payments_one_settlement(self):
        req,_=await self.store.request_payment(2,self.ident,'deposit')
        await self.store.invoice_once()
        await self.store.accept_paykeeper(dict(id='123',orderid=req['id'],clientid='Гость'),req['amount'])
        self.provider.invoice.return_value=('1002','https://paykeeper.example/bill/1002/')
        req,_=await self.store.request_payment(2,self.ident,'balance')
        await self.store.invoice_once()
        await self.store.accept_paykeeper(dict(id='124',orderid=req['id'],clientid='Гость'),req['amount'])
        await self.store.pool.execute("INSERT INTO paykeeper_fiscal_receipts VALUES('98','123','https://paykeeper.example/r/98',now()),('99','124','https://paykeeper.example/r/99',now())")
        row=await self.store.get(2,self.ident)
        self.store.clock=AsyncMock(return_value=row['ends_at']+timedelta(seconds=1))
        result=await self.store.request_final_receipt(1,self.ident)
        self.assertEqual(set(json.loads(result['payment_ids'])),{'123','124'})
        self.assertEqual(result['amount'],row['paid'])

    async def test_missing_initial_receipt_and_refund_blocked(self):
        await self.ready()
        await self.store.pool.execute('DELETE FROM paykeeper_fiscal_receipts')
        with self.assertRaises(RuleError):await self.store.request_final_receipt(1,self.ident)
        await self.store.pool.execute("INSERT INTO paykeeper_fiscal_receipts VALUES('99','123','https://paykeeper.example/r/99',now())")
        async with self.store.tx() as db:await self.store.audit(db,self.ident,1,'refund')
        with self.assertRaises(RuleError):await self.store.request_final_receipt(1,self.ident)

    async def test_reconcile_keeps_same_key(self):
        await self.ready();row=await self.store.request_final_receipt(1,self.ident)
        self.provider.create_settlement.side_effect=TimeoutError()
        await self.store.settlement_once()
        await self.store.reconcile_settlement(1,self.ident,'555')
        await self.store.settlement_once()
        self.assertEqual(await self.store.pool.fetchval('SELECT receipt_key FROM paykeeper_settlements'),row['receipt_key'])
        self.assertEqual(self.provider.create_settlement.await_count,1)

    async def test_admin_form_csrf_and_explicit_confirmation(self):
        await self.ready()
        async with TestClient(TestServer(make_app(self.store,self.settings,secure=False))) as client:
            token=await self.store.login(await self.store.login_token(1))
            client.session.cookie_jar.update_cookies({'pihta_session':token})
            session=await self.store.session(token)
            response=await client.get('/booking/'+self.ident)
            self.assertIn('Услуга оказана — оформить итоговый чек',await response.text())
            path='/booking/'+self.ident+'/settlement'
            response=await client.post(path,data={'service_done':'yes'},headers={'Origin':self.settings.web_url})
            self.assertEqual(response.status,403)
            response=await client.post(path,data={'csrf':session['csrf']},headers={'Origin':self.settings.web_url})
            self.assertEqual(response.status,400)
            response=await client.post(path,data={'csrf':session['csrf'],'service_done':'yes'},headers={'Origin':self.settings.web_url},allow_redirects=False)
            self.assertEqual(response.status,302)
            self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM paykeeper_settlements'),1)

    async def test_interrupted_submission_alerts_without_reissue(self):
        await self.ready();await self.store.request_final_receipt(1,self.ident)
        await self.store.pool.execute("UPDATE paykeeper_settlements SET state='sending',next_check_at=now()-interval '1 minute'")
        await self.store.settlement_once()
        self.provider.create_settlement.assert_not_awaited()
        self.assertEqual(await self.store.pool.fetchval('SELECT state FROM paykeeper_settlements'),'uncertain')
        self.assertTrue(await self.store.pool.fetchval('SELECT 1 FROM outbox WHERE event=$1','pkfinalerror:'+self.ident))

    async def test_temporary_mode_blocks_fiscalisation_and_hides_form(self):
        from dataclasses import replace
        await self.ready();await self.store.request_final_receipt(1,self.ident)
        self.settings=replace(self.settings,paykeeper_settlement_enabled=False)
        self.store.settings=self.settings
        with self.assertRaises(RuleError):await self.store.request_final_receipt(1,self.ident)
        self.assertFalse(await self.store.settlement_once())
        self.provider.create_settlement.assert_not_awaited()
        async with TestClient(TestServer(make_app(self.store,self.settings,secure=False))) as client:
            token=await self.store.login(await self.store.login_token(1))
            client.session.cookie_jar.update_cookies({'pihta_session':token})
            response=await client.get('/booking/'+self.ident)
            self.assertNotIn('Услуга оказана — оформить итоговый чек',await response.text())

    async def test_final_receipt_notification_targets_owner(self):
        from types import SimpleNamespace
        from bot import deliver_once
        await self.ready();await self.store.request_final_receipt(1,self.ident);await self.store.settlement_once()
        await self.store.pool.execute('UPDATE paykeeper_settlements SET next_check_at=now()')
        await self.store.settlement_once()
        await self.store.pool.execute('UPDATE outbox SET delivered_at=now() WHERE event!=$1','pkfinal:'+self.ident)
        bot=SimpleNamespace(send_message=AsyncMock())
        await deliver_once(bot,self.store)
        call=bot.send_message.await_args
        self.assertEqual(call.args[0],2)
        self.assertEqual(call.kwargs['reply_markup'].inline_keyboard[0][0].url,'https://paykeeper.example/receipt/final')
