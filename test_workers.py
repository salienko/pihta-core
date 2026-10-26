"""Worker helpers: notification buttons and channel resolution."""
import os
import unittest
from dataclasses import replace

from core.workers import buttons_for, channels_from_env
from settings import load


class Buttons(unittest.TestCase):
    def setUp(self):
        self.settings = replace(load(), admin_ids=(1,))

    def row(self, recipient=5, event='booking'):
        return {'recipient': recipient, 'booking_id': 'b1', 'event': event}

    def test_guest_gets_booking_button(self):
        rows = buttons_for(self.row(), self.settings)
        self.assertEqual(rows[0][0]['action'], 'open:b1')

    def test_admin_gets_panel_button(self):
        rows = buttons_for(self.row(recipient=1), self.settings)
        self.assertEqual(rows[0][0]['action'], 'staffweb')

    def test_paylink_gives_url_and_check(self):
        payment = {'id': 'r1', 'amount': 200000, 'url': 'https://pay.test/x', 'automatic': True}
        rows = buttons_for(self.row(event='paylink:r1'), self.settings, payment=payment)
        self.assertEqual(rows[0][0]['url'], 'https://pay.test/x')
        self.assertEqual(rows[1][0]['action'], 'paycheck:r1')

    def test_manual_payment_reports_instead_of_checking(self):
        payment = {'id': 'r2', 'amount': 200000, 'url': 'https://pay.test/y', 'automatic': False}
        rows = buttons_for(self.row(event='paylink:r2'), self.settings, payment=payment)
        self.assertEqual(rows[1][0]['action'], 'payreport:r2')

    def test_receipt_button_first(self):
        rows = buttons_for(self.row(event='pkreceipt:r3'), self.settings,
                           receipt_url='https://receipt.test/1')
        self.assertEqual(rows[0][0]['url'], 'https://receipt.test/1')


class Channels(unittest.TestCase):
    def test_reads_both_channels(self):
        settings = replace(load(), admin_ids=(1,))
        os.environ['CORE_API_TOKEN'] = 'x' * 40
        os.environ['CHANNEL_TELEGRAM_URL'] = 'https://tg.test'
        os.environ['CHANNEL_MAX_URL'] = 'https://max.test'
        try:
            found = channels_from_env(settings)
            self.assertEqual(sorted(found), ['max', 'telegram'])
            self.assertEqual(found['telegram']['token'], 'x' * 40)
        finally:
            for key in ('CHANNEL_TELEGRAM_URL', 'CHANNEL_MAX_URL'):
                os.environ.pop(key, None)

    def test_missing_token_disables_delivery(self):
        settings = replace(load(), admin_ids=(1,))
        os.environ['CORE_API_TOKEN'] = ''
        os.environ['CHANNEL_MAX_URL'] = 'https://max.test'
        try:
            self.assertEqual(channels_from_env(settings), {})
        finally:
            os.environ.pop('CHANNEL_MAX_URL', None)
