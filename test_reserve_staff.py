import asyncio
import secrets
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import test_paykeeper as fixtures
from booking_domain import RuleError,payment_confirmation
from booking_reserve import LINK_REMINDER,PAY_REMINDER
from bot import deliver_once
from aiohttp.test_utils import TestClient,TestServer
from admin_web import make_app


@unittest.skipUnless(fixtures.URL,'TEST_DATABASE_URL required')
class ReserveAndStaff(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.Payments.asyncSetUp
    asyncTearDown=fixtures.Payments.asyncTearDown
    linked=fixtures.Payments.linked
    accept=fixtures.Payments.accept

    def at(self,when):self.store.clock=AsyncMock(return_value=when)

    async def window(self):
        await self.linked()
        self.base=(await self.store.get(2,self.ident))['created_at']
        self.at(self.base)
        await self.store.payment_message_sent('paylink:'+self.req['id']+':1',self.ident,2)
        self.at(self.base+timedelta(minutes=10));await self.store.tick()
        await self.store.payment_message_sent(PAY_REMINDER,self.ident,2)
        self.at(self.base+timedelta(minutes=20))
        self.provider.invoice_status.return_value={'status':'created'}

    async def test_first_reminder_once_after_fifteen_minutes(self):
        row=await self.store.get(2,self.ident);base=row['created_at']
        self.at(base+timedelta(minutes=14));await self.store.tick()
        self.assertFalse(await self.store.pool.fetchval('SELECT 1 FROM outbox WHERE event=$1',LINK_REMINDER))
        self.at(base+timedelta(minutes=15));await self.store.tick();await self.store.tick()
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM outbox WHERE event=$1 AND recipient=2',LINK_REMINDER),1)

    async def test_link_warning_and_cancel_at_twenty_minutes(self):
        await self.window()
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM outbox WHERE event=$1 AND recipient=2',PAY_REMINDER),1)
        self.at(self.base+timedelta(minutes=19,seconds=59));self.assertFalse(await self.store.reserve_once())
        self.at(self.base+timedelta(minutes=20));self.assertTrue(await self.store.reserve_once())
        row=await self.store.get(2,self.ident)
        self.assertEqual(row['status'],'cancelled')
        self.assertTrue(await self.store.free(row['starts_at']))
        self.assertEqual(self.provider.invoice_status.await_count,1)

    async def test_last_ten_minutes_start_when_warning_delivered(self):
        await self.window()
        await self.store.pool.execute('UPDATE booking_payment_windows SET warned_at=NULL')
        self.at(self.base+timedelta(minutes=45));self.assertFalse(await self.store.reserve_once())
        await self.store.payment_message_sent(PAY_REMINDER,self.ident,2)
        self.at(self.base+timedelta(minutes=54));self.assertFalse(await self.store.reserve_once())
        self.at(self.base+timedelta(minutes=55));self.assertTrue(await self.store.reserve_once())
        self.assertEqual((await self.store.get(2,self.ident))['status'],'cancelled')

    async def test_any_money_and_payment_during_check_prevent_cancel(self):
        await self.window()
        async def paid_during_check(invoice):
            await self.store.pool.execute('UPDATE bookings SET paid=1 WHERE id=$1',self.ident)
            return {'status':'created'}
        self.provider.invoice_status.side_effect=paid_during_check
        await self.store.reserve_once()
        self.assertEqual((await self.store.get(2,self.ident))['status'],'pending')
        self.at(self.base+timedelta(minutes=30));self.assertFalse(await self.store.reserve_once())
        self.assertFalse(await self.store.reserve_delivery_allowed(self.ident,PAY_REMINDER,2))

    async def test_provider_paid_confirms_instead_of_cancel(self):
        await self.window()
        self.provider.invoice_status.return_value={'status':'paid','paymentid':'123'}
        await self.store.reserve_once()
        row=await self.store.get(2,self.ident)
        self.assertEqual(row['status'],'confirmed');self.assertEqual(row['paid'],200000)

    async def test_network_failure_keeps_reservation_then_retries(self):
        await self.window();self.provider.invoice_status.side_effect=TimeoutError()
        await self.store.reserve_once()
        self.assertEqual((await self.store.get(2,self.ident))['status'],'pending')
        self.provider.invoice_status.side_effect=None
        self.at(self.base+timedelta(minutes=22));await self.store.reserve_once()
        self.assertEqual((await self.store.get(2,self.ident))['status'],'cancelled')

    async def test_new_unknown_invoice_blocks_cancel(self):
        await self.window()
        async def changed(invoice):
            await self.store.create_staff_invoice(1,secrets.token_hex(16),'+79810000000',10000,'full',booking_id=self.ident)
            return {'status':'created'}
        self.provider.invoice_status.side_effect=changed
        await self.store.reserve_once()
        self.assertEqual((await self.store.get(2,self.ident))['status'],'pending')

    async def test_old_links_do_not_acquire_retrospective_timer(self):
        await self.linked()
        await self.store.pool.execute("UPDATE outbox SET delivered_at=now() WHERE event LIKE 'paylink:%'")
        row=await self.store.get(2,self.ident);self.at(row['created_at']+timedelta(hours=1));await self.store.tick()
        self.assertFalse(await self.store.pool.fetchval('SELECT 1 FROM booking_payment_windows'))
        self.assertFalse(await self.store.pool.fetchval('SELECT 1 FROM outbox WHERE event=$1',LINK_REMINDER))
        self.assertFalse(await self.store.reserve_once())

    async def test_failed_link_delivery_does_not_start_clock(self):
        await self.linked()
        await self.store.pool.execute("UPDATE outbox SET delivered_at=now() WHERE event NOT LIKE 'paylink:%'")
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError()))
        await deliver_once(bot,self.store)
        self.assertFalse(await self.store.pool.fetchval('SELECT 1 FROM booking_payment_windows'))
        bot.send_message.side_effect=None
        await self.store.pool.execute("UPDATE outbox SET due_at=now() WHERE event LIKE 'paylink:%'")
        await deliver_once(bot,self.store)
        self.assertTrue(await self.store.pool.fetchval('SELECT 1 FROM booking_payment_windows'))

    async def test_late_payment_is_reviewed_without_restoring_cancelled_booking(self):
        await self.window();await self.store.reserve_once();await self.accept()
        row=await self.store.get(2,self.ident)
        self.assertEqual(row['status'],'cancelled');self.assertEqual(row['paid'],0)
        self.assertEqual(await self.store.pool.fetchval('SELECT disposition FROM paykeeper_payments'),'review')

    async def staff_values(self):
        row=await self.store.get(2,self.ident)
        values=dict(start=row['starts_at']+timedelta(hours=3),phone='+79000000001',guests=5,children=1,name='')
        _,quote=await self.store.staff_quote(1,**values)
        return values,quote['total']

    async def test_staff_phone_booking_is_atomic_and_not_owned_by_phone(self):
        values,amount=await self.staff_values();key=secrets.token_hex(16)
        a,b=await asyncio.gather(self.store.create_staff_booking(1,key,**values,expected_total=amount),self.store.create_staff_booking(1,key,**values,expected_total=amount))
        self.assertEqual(a[0],b[0]);self.assertNotEqual(a[1],b[1])
        row=await self.store.get(1,a[0],staff=True)
        self.assertIsNone(row['telegram_id']);self.assertEqual(row['data']['name'],'Гость')
        self.assertEqual(row['data']['guests'],5);self.assertFalse(row['data']['consent'])
        with self.assertRaises(RuleError):await self.store.get(2,a[0])
        self.assertFalse(await self.store.pool.fetchval('SELECT 1 FROM outbox WHERE booking_id=$1 AND recipient NOT IN (1,9)',a[0]))
        with self.assertRaises(RuleError):await self.store.create_staff_booking(1,secrets.token_hex(16),**values,expected_total=amount)

    async def test_staff_booking_rejects_bad_counts_price_and_nonstaff(self):
        values,amount=await self.staff_values()
        with self.assertRaises(RuleError):await self.store.staff_quote(2,**values)
        with self.assertRaises(RuleError):await self.store.staff_quote(1,**dict(values,guests=9))
        with self.assertRaises(RuleError):await self.store.create_staff_booking(1,secrets.token_hex(16),**values,expected_total=amount-1)

    async def test_phone_booking_invoice_credits_without_sending_to_unrelated_guest(self):
        values,amount=await self.staff_values()
        ident,_=await self.store.create_staff_booking(1,secrets.token_hex(16),**values,expected_total=amount)
        invoice=await self.store.create_staff_invoice(1,secrets.token_hex(16),values['phone'],200000,'full',booking_id=ident)
        await self.store.invoice_once()
        self.assertEqual(self.provider.invoice.await_args.args[0]['client_id'],'Гость')
        await self.store.accept_paykeeper(dict(id='987',orderid=invoice['request_id'],clientid='Гость'),200000)
        self.assertEqual((await self.store.get(1,ident,staff=True))['status'],'confirmed')
        self.assertFalse(await self.store.pool.fetchval('SELECT 1 FROM outbox WHERE booking_id=$1 AND recipient NOT IN (1,9)',ident))

    async def test_admin_form_previews_and_creates_phone_booking(self):
        values,amount=await self.staff_values()
        async with TestClient(TestServer(make_app(self.store,self.settings,secure=False))) as client:
            response=await client.get('/new-booking',allow_redirects=False);self.assertEqual(response.status,302)
            token=await self.store.login(await self.store.login_token(1));client.session.cookie_jar.update_cookies({'pihta_session':token})
            session=await self.store.session(token);headers={'Origin':self.settings.web_url}
            response=await client.get('/new-booking?day='+values['start'].date().isoformat());self.assertEqual(response.status,200)
            self.assertIn('Свободный сеанс',await response.text())
            data=dict(values,start=values['start'].isoformat(),csrf=session['csrf'])
            response=await client.post('/new-booking/preview',data=data,headers=headers)
            self.assertEqual(response.status,200);self.assertIn('Создать бронь',await response.text())
            data.update(expected_total=str(amount),operation=secrets.token_hex(16))
            response=await client.post('/new-booking/create',data=data,headers=headers,allow_redirects=False)
            self.assertEqual(response.status,302)
            self.assertTrue(response.headers['Location'].startswith('/booking/'))
            response=await client.post('/new-booking/create',data=data,allow_redirects=False);self.assertEqual(response.status,403)

    async def test_guest_copy_unpaid_partial_and_full(self):
        await self.linked()
        self.provider.invoice_status.return_value={'status':'created'}
        ui=fixtures.BookingUI(self.settings,self.store);ui.show=AsyncMock();message=SimpleNamespace(answer=AsyncMock())
        callback=SimpleNamespace(from_user=SimpleNamespace(id=2),message=message,data='paycheck:'+self.req['id'])
        await ui._callback(callback)
        self.assertEqual(message.answer.await_args.args[0],'Мы не видим вашей оплаты. Повторите позже.')
        self.at((await self.store.get(2,self.ident))['created_at']+timedelta(seconds=16))
        self.provider.invoice_status.return_value={'status':'paid','paymentid':'123'}
        await ui._callback(callback)
        self.assertEqual(message.answer.await_args.args[0],'Ваша оплата принята. Спасибо. Не забудьте внести оставшуюся сумму до начала сеанса.')
        row=await self.store.get(2,self.ident);row['paid']=row['estimate']['total']
        self.assertEqual(payment_confirmation(row),'Ваша оплата принята. Спасибо.')

    async def test_received_link_suppresses_first_reminder(self):
        await self.window();await self.store.tick()
        self.assertFalse(await self.store.reserve_delivery_allowed(self.ident,LINK_REMINDER,2))
        self.assertFalse(await self.store.pool.fetchval('SELECT 1 FROM outbox WHERE event=$1',LINK_REMINDER))

    async def test_warning_stops_after_any_partial_payment(self):
        await self.window()
        await self.store.pool.execute('UPDATE bookings SET paid=100 WHERE id=$1',self.ident)
        await self.store.tick()
        self.assertFalse(await self.store.reserve_delivery_allowed(self.ident,PAY_REMINDER,2))
        self.assertFalse(await self.store.reserve_once())
        row=await self.store.get(2,self.ident)
        self.at(row['ends_at']+timedelta(seconds=1))
        self.assertEqual((await self.store.get(2,self.ident))['status'],'completed')

    async def test_every_invoice_checked_before_release(self):
        await self.window()
        self.at(self.base)
        req,_=await self.store.request_payment(2,self.ident,'full')
        self.provider.invoice.return_value=('1002','https://paykeeper.example/bill/1002/')
        await self.store.invoice_once()
        self.at(self.base+timedelta(minutes=20))
        self.provider.invoice_status.side_effect=[{'status':'created'},{'status':'paid','paymentid':'124'}]
        await self.store.reserve_once()
        self.assertEqual(self.provider.invoice_status.await_count,2)
        self.assertEqual((await self.store.get(2,self.ident))['status'],'confirmed')

    async def test_repeated_link_does_not_extend_window(self):
        await self.window()
        before=await self.store.pool.fetchval('SELECT link_received_at FROM booking_payment_windows')
        self.at(self.base+timedelta(minutes=18))
        await self.store.payment_message_sent('paylink:'+self.req['id']+':1',self.ident,2)
        self.assertEqual(await self.store.pool.fetchval('SELECT link_received_at FROM booking_payment_windows'),before)

    async def test_two_different_staff_requests_cannot_take_same_slot(self):
        values,amount=await self.staff_values()
        results=await asyncio.gather(self.store.create_staff_booking(1,secrets.token_hex(16),**values,expected_total=amount),self.store.create_staff_booking(9,secrets.token_hex(16),**dict(values,phone='+79000000002'),expected_total=amount),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,RuleError) for r in results),1)
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM bookings WHERE data->>'source'='staff'"),1)

    async def test_same_phone_does_not_link_telegram_profile(self):
        values,_=await self.staff_values()
        row=await self.store.get(2,self.ident)
        values.update(start=row['starts_at']+timedelta(days=1),phone=row['data']['phone'])
        _,quote=await self.store.staff_quote(1,**values)
        ident,_=await self.store.create_staff_booking(1,secrets.token_hex(16),**values,expected_total=quote['total'])
        self.assertIsNone((await self.store.get(1,ident,staff=True))['telegram_id'])
        with self.assertRaises(RuleError):await self.store.get(2,ident)
