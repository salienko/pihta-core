import asyncio
import os
import secrets
import unittest
from dataclasses import replace
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from settings import load
from booking_store import BookingStore
from booking_domain import TZ,RuleError,estimate,card
from booking_payments import payment_url
from booking_ui import BookingUI
from bot import deliver_once

URL=os.getenv('TEST_DATABASE_URL','')


class LinkValidation(unittest.TestCase):
    def test_https_link_only(self):
        self.assertEqual(payment_url('https://pay.example/order?q=1'),'https://pay.example/order?q=1')
        for value in ['http://pay.example','https://user:password@pay.example','https://pay.example x','not-a-link','javascript:alert(1)']:
            with self.assertRaises(RuleError):payment_url(value)


@unittest.skipUnless(URL,'TEST_DATABASE_URL required')
class Payments(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema='test_payment_'+secrets.token_hex(6)
        self.settings=replace(load(),database_url=URL,admin_ids=(1,9),web_url='https://pihta.test')
        self.store=await BookingStore.connect(self.settings,self.schema)
        self.start=(datetime.now(TZ)+timedelta(days=4)).replace(hour=13,minute=0,second=0,microsecond=0)
        token=await self.store.begin(2);d=await self.store.draft(2)
        catalog,holidays=await self.store.pricing()
        data=dict(d['data'],name='Гость',phone='+79810000000',day=self.start.date().isoformat(),start=self.start.isoformat(),consent=True,rules_hash=self.settings.rules_hash)
        data['quote']=estimate(data,catalog,holidays)
        await self.store.save(2,d,'review',data)
        self.ident=(await self.store.submit(2,token))[0]
        self.amount=data['quote']['total']
        self.ui=BookingUI(self.settings,self.store);self.ui.say=AsyncMock()
        self.m=SimpleNamespace(chat=SimpleNamespace(type='private'),from_user=SimpleNamespace(id=2),photo=None,text='',contact=None,answer=AsyncMock())
    async def asyncTearDown(self):
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE');await self.store.pool.close()
    async def linked(self,kind='deposit'):
        req,_=await self.store.request_payment(2,self.ident,kind)
        await self.store.payment_message_delivered(req['id'],1,100+req['amount'])
        await self.store.link_payment(1,100+req['amount'],'https://pay.example/'+req['id'])
        return req
    async def callback(self,data,actor=2):
        await self.ui.callback(SimpleNamespace(message=self.m,from_user=SimpleNamespace(id=actor),data=data,answer=AsyncMock()))
    async def row(self):return await self.store.get(2,self.ident)

    async def test_request_deduplicated_but_both_choices_allowed(self):
        a,created=await self.store.request_payment(2,self.ident,'deposit')
        b,again=await self.store.request_payment(2,self.ident,'deposit')
        full,_=await self.store.request_payment(2,self.ident,'full')
        self.assertTrue(created);self.assertFalse(again);self.assertEqual(a['id'],b['id'])
        self.assertEqual(a['amount'],200000);self.assertEqual(full['amount'],self.amount)
        self.assertEqual((await self.row())['status'],'pending')

    async def test_reply_link_binds_correct_guest_and_persists(self):
        req=await self.linked()
        self.assertEqual((await self.store.payment_requests(2,self.ident))[0]['url'],'https://pay.example/'+req['id'])
        other=await BookingStore.connect(self.settings,self.schema)
        try:self.assertEqual((await other.payment_requests(2,self.ident))[0]['id'],req['id'])
        finally:await other.pool.close()

    async def test_no_guest_or_unknown_message_can_send_link(self):
        await self.linked()
        with self.assertRaises(RuleError):await self.store.link_payment(2,200100,'https://pay.example/a')
        with self.assertRaises(RuleError):await self.store.link_payment(1,999,'https://pay.example/a')

    async def test_report_does_not_credit_money_or_confirm(self):
        req=await self.linked()
        _,first=await self.store.report_payment(2,req['id']);_,again=await self.store.report_payment(2,req['id'])
        row=await self.row();self.assertTrue(first);self.assertFalse(again)
        self.assertEqual(row['paid'],0);self.assertEqual(row['status'],'pending')
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM outbox WHERE event LIKE 'payreported:%'"),2)

    async def test_no_report_before_link_or_for_another_guest(self):
        req,_=await self.store.request_payment(2,self.ident,'deposit')
        with self.assertRaises(RuleError):await self.store.report_payment(2,req['id'])
        req=await self.linked()
        with self.assertRaises(RuleError):await self.store.report_payment(3,req['id'])

    async def test_deposit_confirms_then_balance_completes(self):
        req=await self.linked();await self.store.decide_payment(1,req['id'],True)
        row=await self.row();self.assertEqual(row['paid'],200000);self.assertEqual(row['status'],'confirmed')
        self.assertIn('Внесена предоплата',card(row))
        balance=await self.linked('balance');self.assertEqual(balance['amount'],self.amount-200000)
        text=await self.store.pool.fetchval("SELECT text FROM outbox WHERE event=$1 AND recipient=1",'payreq:'+balance['id'])
        self.assertIn('Внесено: 2 000',text)
        await self.store.decide_payment(1,balance['id'],True)
        row=await self.row();self.assertEqual(row['paid'],self.amount);self.assertEqual(row['status'],'confirmed')
        self.assertIn('Внесена вся стоимость',card(row))

    async def test_full_payment_and_no_more_requests(self):
        req=await self.linked('full');await self.store.decide_payment(1,req['id'],True)
        self.assertEqual((await self.row())['paid'],self.amount)
        with self.assertRaises(RuleError):await self.store.request_payment(2,self.ident,'full')
        await self.ui.show(self.m,2,self.ident)
        self.assertNotIn('payrequest:',str(self.ui.say.call_args));self.assertNotIn('payreport:',str(self.ui.say.call_args))

    async def test_two_admins_cannot_credit_same_receipt_twice(self):
        req=await self.linked()
        results=await asyncio.gather(self.store.decide_payment(1,req['id'],True),self.store.decide_payment(9,req['id'],True))
        self.assertEqual(sum(changed for _,changed in results),1);self.assertEqual((await self.row())['paid'],200000)

    async def test_competing_deposit_and_full_are_not_both_credited(self):
        deposit=await self.linked();full=await self.linked('full')
        await self.store.decide_payment(1,deposit['id'],True)
        with self.assertRaises(RuleError):await self.store.decide_payment(9,full['id'],True)
        self.assertEqual((await self.row())['paid'],200000)

    async def test_missing_payment_allows_new_report_without_duplicate_decision(self):
        req=await self.linked();await self.store.report_payment(2,req['id'])
        await self.store.decide_payment(1,req['id'],False)
        _,again=await self.store.decide_payment(1,req['id'],False);self.assertFalse(again)
        self.assertTrue((await self.store.report_payment(2,req['id']))[1])
        self.assertEqual((await self.row())['paid'],0)

    async def test_paid_cancellation_is_request_until_staff_action(self):
        req=await self.linked();await self.store.decide_payment(1,req['id'],True)
        await self.callback('cancelask:'+self.ident)
        self.assertIn('Ваш запрос передан администратору',self.m.answer.call_args.args[0])
        await self.callback('cancelask:'+self.ident)
        self.assertEqual((await self.row())['status'],'confirmed');self.assertFalse(await self.store.free(self.start))
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM outbox WHERE event='cancel_requested'"),2)
        with self.assertRaises(RuleError):await self.store.transition(2,self.ident,'cancel')
        await self.store.transition(1,self.ident,'cancel','По запросу гостя',staff=True)
        self.assertEqual((await self.row())['status'],'cancelled');self.assertEqual((await self.row())['paid'],200000)
        self.assertTrue(await self.store.free(self.start))

    async def test_cancelled_request_cannot_be_credited(self):
        req=await self.linked();await self.store.transition(2,self.ident,'cancel')
        with self.assertRaises(RuleError):await self.store.decide_payment(1,req['id'],True)
        self.assertEqual((await self.row())['paid'],0)

    async def test_both_buttons_remain_after_link_and_report(self):
        req=await self.linked();await self.store.report_payment(2,req['id'])
        await self.ui.show(self.m,2,self.ident)
        buttons=str(self.ui.say.call_args.args[2])
        for value in ['payrequest:deposit:','payrequest:full:','payreport:']:self.assertIn(value,buttons)
        await self.store.decide_payment(1,req['id'],True);await self.ui.show(self.m,2,self.ident)
        buttons=str(self.ui.say.call_args.args[2]);self.assertIn('payrequest:balance:',buttons);self.assertNotIn('payrequest:deposit:',buttons)

    async def test_web_payment_confirms_and_invalidates_old_link(self):
        req=await self.linked();await self.store.accounting(1,self.ident,'webpayment','payment',amount=200000,note='Получена предоплата')
        self.assertEqual((await self.row())['status'],'confirmed')
        with self.assertRaises(RuleError):await self.store.decide_payment(1,req['id'],True)

    async def test_staff_confirmation_without_money_is_rejected(self):
        with self.assertRaises(RuleError):await self.store.transition(1,self.ident,'confirm',staff=True)

    async def test_delivery_records_reply_mapping(self):
        req,_=await self.store.request_payment(2,self.ident,'deposit')
        await self.store.pool.execute("UPDATE outbox SET delivered_at=now() WHERE event NOT LIKE 'payreq:%'")
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
        await deliver_once(bot,self.store)
        self.assertEqual(await self.store.pool.fetchval('SELECT request_id FROM payment_messages WHERE message_id=42'),req['id'])
        self.assertIn('paystaff:yes:',str(bot.send_message.call_args))

    async def test_expired_booking_skips_old_link_delivery(self):
        req=await self.linked()
        await self.store.pool.execute("UPDATE bookings SET status='expired' WHERE id=$1",self.ident)
        self.assertIsNone(await self.store.payment_delivery('paylink:'+req['id']+':1'))

    async def test_admin_reply_handler_sends_link_without_changing_customer_draft(self):
        req,_=await self.store.request_payment(2,self.ident,'deposit')
        await self.store.payment_message_delivered(req['id'],1,77)
        message=SimpleNamespace(chat=SimpleNamespace(type='private'),from_user=SimpleNamespace(id=1),
            text='https://pay.example/receipt',reply_to_message=SimpleNamespace(message_id=77),entities=[],answer=AsyncMock())
        await self.ui.message(message)
        self.assertIn('Ссылка передана гостю',message.answer.call_args.args[0])
        self.assertEqual((await self.store.payment_requests(2,self.ident))[0]['url'],'https://pay.example/receipt')

    async def test_ui_report_acknowledges_and_does_not_confirm(self):
        req=await self.linked();await self.callback('payreport:'+req['id'])
        self.assertEqual(self.m.answer.call_args.args[0],'Администратор получил ваше обращение и скоро ответит.')
        self.assertEqual((await self.row())['status'],'pending')

    async def test_guest_cannot_press_staff_receipt_button(self):
        req=await self.linked();await self.callback('paystaff:yes:'+req['id'])
        self.assertEqual((await self.row())['paid'],0)
        self.assertIn('Нет доступа',self.ui.say.call_args.args[1])

    async def test_confirmation_retains_immutable_booking_snapshot(self):
        before=await self.row();req=await self.linked()
        await self.store.decide_payment(1,req['id'],True);after=await self.row()
        for field in ('data','estimate','starts_at','ends_at','rules_hash','created_at'):
            self.assertEqual(before[field],after[field])
