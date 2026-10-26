import asyncio
from dataclasses import replace
from datetime import datetime,timedelta
import os
import json
import secrets
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
from settings import load
from booking_domain import TZ,RuleError,estimate,validate_start,can_cancel
from booking_store import BookingStore
from booking_ui import BookingUI
from admin_web import make_app,money

class Policies(unittest.TestCase):
    def setUp(self):
        self.clock=datetime(2026,10,21,10,tzinfo=TZ);self.settings=load();self.catalog={i['code']:i for i in self.settings.content['services']}
        self.data=dict(start='2026-10-28T13:00:00+03:00',hours=2,guests=6,children=2,cart={})
    def test_child_and_sheet_rules(self):self.assertEqual(estimate(self.data,self.catalog,{})['total'],500000)
    def test_extra_guests(self):self.data['children']=0;self.assertEqual(estimate(self.data,self.catalog,{})['guest_extra'],100000)
    def test_capacity(self):
        self.data['guests']=9
        with self.assertRaises(RuleError):estimate(self.data,self.catalog,{})
    def test_aroma_mutual_exclusion(self):
        self.data['cart']={'eucalyptus':1,'lavender':1}
        with self.assertRaises(RuleError):estimate(self.data,self.catalog,{})
    def test_double_aroma(self):
        self.data['cart']={'lavender':2}
        with self.assertRaises(RuleError):estimate(self.data,self.catalog,{})
    def test_evening(self):self.data['start']='2026-10-28T19:00:00+03:00';self.assertEqual(estimate(self.data,self.catalog,{})['rent'],600000)
    def test_holiday(self):self.assertEqual(estimate(self.data,self.catalog,{'2026-10-28':{'base':650000}})['rent'],650000)
    def test_confirmed_cancellation_always_requires_staff(self):self.assertFalse(can_cancel('confirmed',self.clock+timedelta(days=4),self.clock));self.assertFalse(can_cancel('confirmed',self.clock+timedelta(hours=23),self.clock))
    def test_pending_cancel_anytime(self):self.assertTrue(can_cancel('pending',self.clock-timedelta(hours=1),self.clock))
    def test_exact_hour(self):validate_start(self.clock+timedelta(hours=3),self.clock+timedelta(hours=2))
    def test_late_submission(self):
        with self.assertRaises(RuleError):validate_start(self.clock,self.clock)
    def test_nonstandard(self):
        with self.assertRaises(RuleError):validate_start(self.clock+timedelta(hours=2),self.clock)
    def test_money(self):self.assertEqual(money('100.50'),10050)
    def test_money_fraction_and_nan(self):
        for v in ['1.001','NaN','Infinity','-1']:
            with self.assertRaises(RuleError):money(v)

URL=os.getenv('TEST_DATABASE_URL','')
@unittest.skipUnless(URL,'TEST_DATABASE_URL is required for PostgreSQL tests')
class Database(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema='test_booking_'+secrets.token_hex(8);self.settings=replace(load(),database_url=URL,admin_ids=(1,),web_url='https://pihta.test')
        self.store=await BookingStore.connect(self.settings,self.schema)
        self.start=(datetime.now(TZ)+timedelta(days=4)).replace(hour=13,minute=0,second=0,microsecond=0)
        self.catalog={i['code']:i for i in self.settings.content['services']}
    async def asyncTearDown(self):
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE');await self.store.pool.close()
    async def ready(self,actor=2,delta=0):
        t=await self.store.begin(actor);d=await self.store.draft(actor,t)
        data=dict(start=(self.start+timedelta(days=delta)).isoformat(),hours=2,guests=2,children=0,cart={},flow_version=3,day=(self.start+timedelta(days=delta)).date().isoformat(),name='Иван',phone='+79812440050',consent=True,rules_hash=self.settings.rules_hash)
        data['quote']=estimate(data,self.catalog,{})
        await self.store.save(actor,d,'review',data);return t
    async def book(self,actor=2,delta=0):return (await self.store.submit(actor,await self.ready(actor,delta)))[0]
    async def test_migrate_legacy_draft_preserves_contacts_but_restarts_journey(self):
        token=await self.ready();d=await self.store.draft(2,token)
        data=d['data'];data.pop('flow_version');data['cart']={'oak':2}
        await self.store.save(2,d,'services',data)
        ui=BookingUI(self.settings,self.store);ui.say=AsyncMock()
        await ui.render(None,2,await self.store.draft(2,token))
        d=await self.store.draft(2,token)
        self.assertEqual(d['state'],'name');self.assertEqual(d['data']['cart'],{})
        self.assertFalse(d['data']['consent']);self.assertEqual(d['data']['name'],'Иван')

    async def test_rental_rejects_goods_in_backend(self):
        token=await self.ready();d=await self.store.draft(2,token)
        d['data']['cart']={'oak':1};await self.store.save(2,d,'review',d['data'])
        with self.assertRaisesRegex(RuleError,'только аренду'):await self.store.submit(2,token)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM bookings'),0)

    async def test_submit_revision_is_checked_atomically(self):
        token=await self.ready();d=await self.store.draft(2,token)
        await self.store.save(2,d,'review',d['data'])
        with self.assertRaises(RuleError):await self.store.submit(2,token,expected_revision=d['revision'])
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM bookings'),0)

    async def test_concurrent_slot_only_one_winner(self):
        a,b=await self.ready(2),await self.ready(3)
        results=await asyncio.gather(self.store.submit(2,a),self.store.submit(3,b),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,tuple) for r in results),1);self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM bookings'),1)
    async def test_duplicate_submit(self):
        t=await self.ready();a,b=await asyncio.gather(self.store.submit(2,t),self.store.submit(2,t));self.assertEqual(a[0],b[0]);self.assertNotEqual(a[1],b[1])
    async def test_two_limit(self):
        await self.book(2,0);await self.book(2,1);t=await self.ready(2,2)
        with self.assertRaises(RuleError):await self.store.submit(2,t)
    async def test_same_date(self):
        await self.book();t=await self.ready();d=await self.store.draft(2,t);data=d['data'];data['start']=(self.start+timedelta(hours=3)).isoformat();data['quote']=estimate(data,self.catalog,{});await self.store.save(2,d,'review',data)
        with self.assertRaises(RuleError):await self.store.submit(2,t)
    async def test_cancel_release_and_history(self):
        ident=await self.book();await self.store.transition(2,ident,'cancel');self.assertTrue(await self.store.free(self.start));self.assertEqual((await self.store.get(2,ident))['status'],'cancelled');self.assertEqual(len(await self.store.listing(2,True)),1)
    async def test_confirmed_inside_day_rejects_cancel(self):
        ident=await self.book();await self.store.accounting(1,ident,'deposit','payment',amount=200000,note='предоплата')
        with self.assertRaises(RuleError):await self.store.transition(2,ident,'cancel',clock=self.start-timedelta(hours=23))
    async def test_pending_inside_day_can_cancel(self):ident=await self.book();await self.store.transition(2,ident,'cancel',clock=self.start-timedelta(minutes=30))
    async def test_expire_cannot_confirm(self):
        ident=await self.book()
        with self.assertRaises(RuleError):await self.store.transition(1,ident,'confirm',clock=self.start+timedelta(hours=2),staff=True)
        # Exception rolls back its transaction; the scheduled expiry persists separately.
        async with self.store.tx() as db:await self.store.expire(db,self.start+timedelta(hours=2))
        self.assertEqual((await self.store.get(2,ident))['status'],'expired')
    async def test_confirm_cancel_race_no_revival(self):
        ident=await self.book();await self.store.transition(2,ident,'cancel')
        with self.assertRaises(RuleError):await self.store.transition(1,ident,'confirm',staff=True)
    async def test_other_user_cannot_read_or_cancel(self):
        ident=await self.book()
        with self.assertRaises(RuleError):await self.store.get(3,ident)
        with self.assertRaises(RuleError):await self.store.transition(3,ident,'cancel')
    async def test_admin_reason_required(self):
        ident=await self.book()
        with self.assertRaises(RuleError):await self.store.transition(1,ident,'cancel',staff=True)
    async def test_snapshot_immutable(self):
        ident=await self.book()
        with self.assertRaises(Exception):await self.store.pool.execute("UPDATE bookings SET data='{}' WHERE id=$1",ident)
    async def test_payment_idempotent_and_refund(self):
        ident=await self.book();await self.store.accounting(1,ident,'same','payment',amount=10000,note='наличные');await self.store.accounting(1,ident,'same','payment',amount=10000,note='наличные')
        self.assertEqual((await self.store.get(2,ident))['paid'],10000)
        with self.assertRaises(RuleError):await self.store.accounting(1,ident,'refund','refund',amount=20000,note='возврат')
    async def test_actual_preserves_snapshot_and_minimum(self):
        ident=await self.book();original=(await self.store.get(2,ident))['data']
        with self.assertRaises(RuleError):await self.store.accounting(1,ident,'actual1','actual',guests=1,total=1,note='меньше')
        await self.store.accounting(1,ident,'actual2','actual',guests=3,total=700000,note='фактический расчёт');self.assertEqual((await self.store.get(2,ident))['data'],original)
    async def test_profile_does_not_change_booking(self):
        ident=await self.book();await self.store.profile(2,'Пётр','+79810000000');self.assertEqual((await self.store.get(2,ident))['data']['name'],'Иван')
    async def test_stale_draft_cannot_edit_submitted(self):
        t=await self.ready();d=await self.store.draft(2,t);await self.store.submit(2,t)
        with self.assertRaises(RuleError):await self.store.save(2,d,'services',d['data'])
    async def test_login_one_time_and_role(self):
        t=await self.store.login_token(1);session=await self.store.login(t);self.assertEqual((await self.store.session(session))['actor'],1)
        with self.assertRaises(RuleError):await self.store.login(t)
        with self.assertRaises(RuleError):await self.store.login_token(2)
    async def test_web_security(self):
        client=TestClient(TestServer(make_app(self.store,self.settings,secure=False)));await client.start_server()
        try:
            r=await client.get('/',allow_redirects=False);self.assertEqual(r.status,302)
            token=await self.store.login_token(1);r=await client.post('/login',data={'token':token},headers={'Origin':'https://evil.test'},allow_redirects=False);self.assertEqual(r.status,403)
            session=await self.store.login(token);client.session.cookie_jar.update_cookies({'pihta_session':session})
            r=await client.get('/');self.assertEqual(r.status,200);self.assertIn('no-store',r.headers['Cache-Control']);self.assertIn('frame-ancestors',r.headers['Content-Security-Policy'])
            ident=await self.book();r=await client.post('/booking/'+ident+'/confirm',data={'csrf':'wrong'},headers={'Origin':self.settings.web_url});self.assertEqual(r.status,403)
        finally:await client.close()
    async def test_login_form_policy_and_strict_origin(self):
        client=TestClient(TestServer(make_app(self.store,self.settings,secure=True)));await client.start_server()
        try:
            response=await client.get('/login')
            self.assertEqual(response.headers['Referrer-Policy'],'same-origin')
            for origin in (None,'null','https://evil.test'):
                token=await self.store.login_token(1)
                headers={'Origin':origin} if origin else {}
                response=await client.post('/login',data={'token':token},headers=headers,allow_redirects=False)
                self.assertEqual(response.status,403)
                # Rejection did not consume the token. A valid same-origin POST can use it.
                response=await client.post('/login',data={'token':token},headers={'Origin':self.settings.web_url},allow_redirects=False)
                self.assertEqual(response.status,302)
                cookie=response.cookies['pihta_session']
                self.assertTrue(cookie['secure']);self.assertTrue(cookie['httponly']);self.assertEqual(cookie['samesite'],'Strict')
                response=await client.post('/login',data={'token':token},headers={'Origin':self.settings.web_url},allow_redirects=False)
                self.assertEqual(response.status,400)
            session=await self.store.login(await self.store.login_token(1))
            client.session.cookie_jar.update_cookies({'pihta_session':session})
            csrf=(await self.store.session(session))['csrf']
            for origin in (None,'null','https://evil.test'):
                response=await client.post('/logout',data={'csrf':csrf},headers={'Origin':origin} if origin else {},allow_redirects=False)
                self.assertEqual(response.status,403)
            self.assertIsNotNone(await self.store.session(session))
        finally:await client.close()

    async def test_old_inventory_does_not_affect_rental(self):
        t=await self.ready();await self.store.pool.execute("INSERT INTO service_settings VALUES('oak',false)")
        ident,_=await self.store.submit(2,t)
        self.assertEqual((await self.store.get(2,ident))['estimate']['items'],[])
    async def test_price_change_requires_new_consent(self):
        t=await self.ready();await self.store.pool.execute('INSERT INTO holiday_rates VALUES($1,$2,$3,$4)',self.start.date(),'Праздник',650000,300000)
        with self.assertRaises(RuleError):await self.store.submit(2,t)
    async def test_cleanup_blocks_overlap_and_exact_boundary_allowed(self):
        ident=await self.book()
        self.assertFalse(await self.store.blocked(self.store.pool,self.start+timedelta(hours=3),self.start+timedelta(hours=5)))
        self.assertTrue(await self.store.blocked(self.store.pool,self.start+timedelta(hours=2),self.start+timedelta(hours=3)))
    async def test_notifications_survive_reconnect(self):
        ident=await self.book();count=await self.store.pool.fetchval('SELECT count(*) FROM outbox')
        other=await BookingStore.connect(self.settings,self.schema)
        try:self.assertEqual(await other.pool.fetchval('SELECT count(*) FROM outbox'),count)
        finally:await other.pool.close()
    async def test_unauthorized_staff_cannot_take_payment(self):
        ident=await self.book()
        with self.assertRaises(RuleError):await self.store.accounting(9,ident,'fake','payment',amount=10000,note='оплата')
    async def test_backup_consistent_and_no_web_tokens(self):
        import tempfile,gzip
        from backup import snapshot
        ident=await self.book();await self.store.login_token(1)
        with tempfile.TemporaryDirectory() as folder:
            path=await snapshot(URL,folder,self.schema)
            with gzip.open(path,'rt',encoding='utf-8') as f:data=json.load(f)
            self.assertEqual(data['tables']['bookings'][0]['id'],ident);self.assertNotIn('web_tokens',data['tables']);self.assertEqual(data['format'],2)
    async def test_message_does_not_edit_booking(self):
        ident=await self.book();original=(await self.store.get(2,ident))['data']
        await self.store.staff_note(1,ident,'note1','Уточните наличие по телефону.')
        await self.store.staff_note(1,ident,'note1','Уточните наличие по телефону.')
        self.assertEqual((await self.store.get(2,ident))['data'],original)
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM outbox WHERE event='note:note1'"),1)

    def make_ui(self):
        from types import SimpleNamespace
        self.ui=BookingUI(self.settings,self.store);self.ui.say=AsyncMock()
        self.m=SimpleNamespace(chat=SimpleNamespace(type='private'),from_user=SimpleNamespace(id=2),text='',contact=None,answer=AsyncMock(),photo=None)
        return self.ui,self.m

    async def click(self,action,value='',raw=False):
        from types import SimpleNamespace
        from booking_flow import callback
        data=action if raw else callback(action,await self.store.draft(2),value)
        await self.ui.callback(SimpleNamespace(message=self.m,from_user=self.m.from_user,data=data,answer=AsyncMock()))

    async def enter(self,text):
        self.m.text=text;self.m.contact=None;await self.ui.message(self.m)

    async def fill_journey(self):
        self.make_ui();await self.click('begin',raw=True)
        self.assertEqual((await self.store.draft(2))['state'],'name')
        await self.enter('Иван');self.assertEqual((await self.store.draft(2))['state'],'phone_contact')
        await self.enter('+79812440050');self.assertEqual((await self.store.draft(2))['state'],'guests')
        await self.click('guest','paid+');await self.click('next','date')
        self.assertEqual((await self.store.draft(2))['state'],'date')
        await self.click('day',self.start.date().isoformat());await self.click('slot','13')
        self.assertEqual((await self.store.draft(2))['state'],'review')

    async def test_full_customer_path(self):
        await self.fill_journey()
        await self.click('consent')
        from booking_flow import callback
        submit=callback('submit',await self.store.draft(2))
        await self.click(submit,raw=True);await self.click(submit,raw=True)
        rows=await self.store.listing(2)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['data']['guests'],2)
        self.assertEqual(rows[0]['data']['cart'],{});self.assertEqual(rows[0]['status'],'pending')
        await self.store.accounting(1,rows[0]['id'],'deposit','payment',amount=200000,note='предоплата')
        await self.click('cancel:'+rows[0]['id'],raw=True)
        self.assertEqual((await self.store.get(2,rows[0]['id']))['status'],'confirmed')
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM outbox WHERE event='cancel_requested'"),1)

    async def test_review_edit_and_return_requires_consent(self):
        await self.fill_journey();await self.click('consent')
        await self.click('edit','guests');await self.click('guest','paid+')
        await self.click('next','date');d=await self.store.draft(2)
        self.assertEqual(d['state'],'review');self.assertEqual(d['data']['guests'],3)
        self.assertFalse(d['data']['consent'])
        await self.click('edit','name');await self.enter('Пётр')
        d=await self.store.draft(2);self.assertEqual(d['state'],'review');self.assertEqual(d['data']['name'],'Пётр')

    async def test_stale_counter_does_not_change_current_draft(self):
        await self.fill_journey();await self.click('edit','guests')
        from booking_flow import callback
        old=callback('guest',await self.store.draft(2),'paid+')
        await self.click(old,raw=True);await self.click(old,raw=True)
        self.assertEqual((await self.store.draft(2))['data']['guests'],3)

    async def test_wrong_contact_rejected_own_contact_accepted(self):
        from types import SimpleNamespace
        self.make_ui();await self.click('begin',raw=True);await self.enter('Иван')
        self.m.contact=SimpleNamespace(user_id=3,phone_number='79812440050');await self.ui.message(self.m)
        self.assertEqual((await self.store.draft(2))['state'],'phone_contact')
        self.m.contact.user_id=2;await self.ui.message(self.m)
        self.assertEqual((await self.store.draft(2))['state'],'guests')
        self.assertEqual(self.m.answer.call_args.kwargs['reply_markup'].remove_keyboard,True)

    async def test_menu_resume_preserves_date_and_counts(self):
        await self.fill_journey();before=(await self.store.draft(2))['data']
        await self.click('home',raw=True);await self.click('resume',raw=True)
        d=await self.store.draft(2)
        self.assertEqual(d['state'],'review');self.assertEqual(d['data']['start'],before['start'])
        self.assertEqual(d['data']['guests'],before['guests'])

    async def test_discard_draft_does_not_cancel_booking(self):
        ident=await self.book();self.make_ui();await self.click('begin',raw=True)
        await self.click('discardask');await self.click('discard')
        with self.assertRaises(RuleError):await self.store.draft(2)
        self.assertEqual((await self.store.get(2,ident))['status'],'pending')
        await self.click('begin',raw=True)
        self.assertEqual((await self.store.draft(2))['state'],'name')

    async def test_profile_edit_cancel_and_save_preserve_booking_draft(self):
        await self.fill_journey();before=(await self.store.draft(2))['data']
        await self.click('profileedit',raw=True);await self.enter('Новое имя')
        await self.click('profilecancel',raw=True)
        self.assertEqual((await self.store.draft(2))['data']['name'],before['name'])
        await self.click('profileedit',raw=True);await self.enter('Пётр');await self.enter('+79810000000')
        self.assertEqual((await self.store.profile(2))['name'],'Пётр')
        d=await self.store.draft(2);self.assertEqual(d['state'],'review')
        self.assertEqual(d['data']['name'],before['name']);self.assertEqual(d['data']['phone'],before['phone'])

    async def test_changed_date_drops_old_time(self):
        await self.fill_journey();await self.click('edit','date')
        await self.click('day',(self.start+timedelta(days=1)).date().isoformat())
        d=await self.store.draft(2);self.assertEqual(d['state'],'slot');self.assertNotIn('start',d['data'])

    async def test_occupied_slot_preserves_contacts_and_offers_another(self):
        await self.fill_journey();await self.book(3)
        await self.click('resume',raw=True)
        d=await self.store.draft(2);self.assertEqual(d['state'],'slot');self.assertEqual(d['data']['name'],'Иван')

    async def test_each_booking_screen_has_valid_telegram_buttons(self):
        await self.fill_journey()
        from booking_ui import kb
        for call in self.ui.say.call_args_list:
            text=call.args[1];rows=call.args[2]
            reply=call.kwargs.get('reply_markup')
            if reply is not None:
                self.assertTrue(any(button.request_contact for row in reply.keyboard for button in row))
                self.assertTrue(any(button.text=='🏠 Главное меню' for row in reply.keyboard for button in row))
                continue
            markup=kb(rows)
            self.assertLessEqual(len(text),1000)
            self.assertTrue(any(button.callback_data=='home' for row in markup.inline_keyboard for button in row))
            for row in markup.inline_keyboard:
                for button in row:
                    if button.callback_data:self.assertLessEqual(len(button.callback_data.encode()),64)

    async def test_counter_edits_caption_without_reuploading_photo(self):
        from types import SimpleNamespace
        ui=BookingUI(self.settings,self.store)
        m=SimpleNamespace(photo=True,edit_caption=AsyncMock(),edit_media=AsyncMock())
        await ui.say(m,'Гостей: 3',[[('Главное меню','home')]],photo='guests',edit='caption')
        m.edit_caption.assert_awaited_once();m.edit_media.assert_not_awaited()

    async def test_admin_filters_pending_separately_from_confirmed(self):
        first=await self.book(2);second=await self.book(3,1)
        await self.store.accounting(1,first,'deposit','payment',amount=200000,note='предоплата')
        rows=await self.store.listing(1,staff=True,status_filter='pending')
        self.assertEqual([r['id'] for r in rows],[second])

    async def test_phone_sharing_is_available_on_first_card(self):
        self.make_ui();await self.click('begin',raw=True);await self.enter('Иван')
        call=self.ui.say.call_args
        self.assertIn('Укажите ваш номер телефона',call.args[1])
        self.assertNotIn('Разрешаете',call.args[1])
        keyboard=call.kwargs['reply_markup']
        self.assertTrue(any(b.request_contact for row in keyboard.keyboard for b in row))
        self.assertFalse(any('share:' in value for row in call.args[2] for _,value in row))
        self.assertNotIn('Иван',call.args[1]);self.assertNotRegex(call.args[1],r'\d из 6')

    async def test_phone_back_saved_number_and_keyboard_departure(self):
        await self.store.profile(2,'Пётр','+79810000000')
        self.make_ui();await self.click('begin',raw=True);await self.enter('Иван')
        self.assertEqual((await self.store.draft(2))['state'],'phone')
        self.assertIsNone(self.ui.say.call_args.kwargs.get('reply_markup'))
        self.assertIn('📱 Использовать мой номер',str(self.ui.say.call_args.args[2]))
        await self.click('back','name');self.assertEqual((await self.store.draft(2))['state'],'name')
        await self.enter('Иван');await self.click('keep','phone')
        d=await self.store.draft(2)
        self.assertEqual(d['state'],'guests');self.assertEqual(d['data']['phone'],'+79810000000')

    async def test_only_current_question_until_review(self):
        self.make_ui();await self.click('begin',raw=True)
        self.assertEqual(self.ui.say.call_args.args[2],[[('🏠 Главное меню','home')]])
        await self.enter('Иван');self.assertNotIn('Иван',self.ui.say.call_args.args[1])
        await self.enter('+79812440050');self.assertNotIn('+79812440050',self.ui.say.call_args.args[1])
        await self.click('next','date');self.assertNotIn('Иван',self.ui.say.call_args.args[1])
        await self.click('day',self.start.date().isoformat());await self.click('slot','13')
        self.assertIn('Иван',self.ui.say.call_args.args[1])
        labels=str(self.ui.say.call_args.args[2])
        self.assertNotIn('Изменить данные',labels);self.assertNotIn('Принимаю правила',labels)
        self.assertIn('Подтверждая бронирование',self.ui.say.call_args.args[1])
        await self.click('submit')
        rows=await self.store.listing(2)
        self.assertEqual(len(rows),1);self.assertTrue(rows[0]['data']['consent'])
        self.assertEqual(rows[0]['rules_hash'],self.settings.rules_hash)

    async def test_calendar_hides_fully_blocked_day_not_partial_day(self):
        day=self.start.date()
        self.assertIn(day,await self.store.available_days([day]))
        await self.book(3)
        self.assertIn(day,await self.store.available_days([day]))
        await self.store.pool.execute("INSERT INTO calendar_blocks(starts_at,ends_at,reason,actor) VALUES($1,$2,'test',1)",self.start.replace(hour=0),self.start.replace(hour=0)+timedelta(days=1))
        self.assertNotIn(day,await self.store.available_days([day]))

    async def test_submit_button_rechecks_changed_price_without_checkbox(self):
        await self.fill_journey()
        await self.store.pool.execute("INSERT INTO holiday_rates(day,label,base,extra) VALUES($1,'test',650000,300000)",self.start.date())
        await self.click('submit')
        self.assertEqual(await self.store.listing(2),[])
        self.assertIn('Расчёт изменился',self.ui.say.call_args.args[1])

    async def test_uniform_back_labels_and_no_step_numbers(self):
        await self.fill_journey();await self.click('editmenu')
        for call in self.ui.say.call_args_list:
            self.assertNotRegex(call.args[1],r'\d из 6')
            for row in call.args[2]:
                for text,_ in row:
                    if text.startswith('←'):self.assertEqual(text,'← Назад')

    async def test_no_extras_in_customer_or_admin_navigation(self):
        await self.fill_journey()
        labels=str(self.ui.say.call_args)
        self.assertNotIn('Дополнительные услуги',labels)
        client=TestClient(TestServer(make_app(self.store,self.settings,secure=False)));await client.start_server()
        try:
            session=await self.store.login(await self.store.login_token(1));client.session.cookie_jar.update_cookies({'pihta_session':session})
            r=await client.get('/catalog');text=await r.text()
            self.assertNotIn('Снять с продажи',text);self.assertIn('Тарифы и праздничные даты',text)
            r=await client.post('/catalog/oak',data={});self.assertEqual(r.status,404)
        finally:await client.close()


class FlowPolicies(unittest.TestCase):
    def test_counts_boundaries_and_children(self):
        from booking_flow import change_guests
        self.assertEqual(change_guests(dict(guests=4,children=1),'kid',1)['guests'],5)
        for data,category,delta in [(dict(guests=8,children=1),'paid',1),(dict(guests=1,children=0),'paid',-1),(dict(guests=2,children=0),'kid',-1)]:
            with self.assertRaises(RuleError):change_guests(data,category,delta)
    def test_callback_limit(self):
        from booking_flow import callback
        self.assertLessEqual(len(callback('resume_step',dict(token='a'*12,revision=999999), '2026-12-31').encode()),64)


class ExtensionContracts(unittest.TestCase):
    def test_no_optional_features_in_first_release(self):
        from bot_features import configured_features
        features=configured_features(None,None)
        self.assertEqual(features.menu(),[])
        self.assertEqual([c.command for c in features.commands()],['start','book','bookings','help'])
    def test_duplicate_names_and_commands_rejected(self):
        from bot_features import Feature,Features
        from aiogram import Router
        from aiogram.types import BotCommand
        with self.assertRaises(ValueError):Features([Feature('future','Новый раздел',Router()),Feature('future','Ещё',Router())])
        with self.assertRaises(ValueError):Features([Feature('future','Новый раздел',Router(),(BotCommand(command='book',description='Конфликт'),))])
    def test_separate_module_registers_without_changing_core(self):
        from bot_features import Feature,Features
        from aiogram import Router,Dispatcher
        core=Router();extension=Router();features=Features([Feature('future','Новый раздел',extension)])
        dp=Dispatcher();features.mount(dp,core)
        self.assertEqual(dp.sub_routers,[extension,core]);self.assertEqual(features.menu(),[[('Новый раздел','feature:future:home')]])
