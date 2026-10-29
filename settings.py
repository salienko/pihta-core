"""Read local content and environment without contacting external services."""
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import os
import re

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: tuple[int, ...]
    database_url: str
    content: dict
    rules: str
    rules_hash: str
    web_url: str = ''
    paykeeper_enabled: bool = False
    paykeeper_url: str = ''
    paykeeper_user: str = ''
    paykeeper_password: str = field(default='', repr=False)
    paykeeper_secret: str = field(default='', repr=False)
    paykeeper_tax: str = ''
    paykeeper_deposit_type: str = ''
    paykeeper_full_type: str = ''
    paykeeper_balance_type: str = ''
    paykeeper_settlement_enabled: bool = False
    # Quiet mode: form the settlement receipt and keep its link, but send no
    # messages to the guest or the admins. On by default — the owner asked for
    # receipts to accumulate silently first; set PAYKEEPER_SETTLEMENT_QUIET=0
    # to turn the messages back on.
    paykeeper_settlement_quiet: bool = True


def load():
    content = json.loads((ROOT / 'content.json').read_text(encoding='utf-8'))
    rules = (ROOT / 'rules.txt').read_text(encoding='utf-8')
    codes = set()
    for item in content['services']:
        if not re.fullmatch(r'[a-z_]{1,20}', item['code']) or item['code'] in codes:
            raise ValueError('Проверьте коды услуг в content.json.')
        codes.add(item['code'])
        if not 1 <= len(item['name']) <= 80 or type(item['price']) is not int or not 0 <= item['price'] <= 100000000:
            raise ValueError('Проверьте названия и цены услуг в content.json.')
    if len(codes) > 20:
        raise ValueError('Поддерживается до 20 услуг в каталоге.')
    for day, rate in content.get('holidays', {}).items():
        from datetime import date
        date.fromisoformat(day)
        if any(type(rate[key]) is not int or rate[key] <= 0 for key in ('base', 'extra')) or rate['base'] % 2:
            raise ValueError('Проверьте праздничный тариф.')
        if not isinstance(rate.get('label'), str) or not 1 <= len(rate['label']) <= 80:
            raise ValueError('Проверьте название праздничного тарифа.')
    admin_values = ','.join(filter(None,(os.getenv('ADMIN_TELEGRAM_IDS', '').strip(),os.getenv('OWNER_TELEGRAM_ID', '').strip())))
    ids = tuple(dict.fromkeys(int(value.strip()) for value in admin_values.split(',') if value.strip()))
    if any(value <= 0 for value in ids):
        raise ValueError('Нужны положительные Telegram ID администраторов.')
    from urllib.parse import urlparse
    web_url=os.getenv('WEB_BASE_URL','').strip().rstrip('/')
    if web_url:
        parsed=urlparse(web_url)
        if parsed.scheme!='https' or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('WEB_BASE_URL must be an HTTPS origin')
    enabled=os.getenv('PAYKEEPER_ENABLED','0')=='1'
    pk_url=os.getenv('PAYKEEPER_URL','').strip().rstrip('/')
    pk_user=os.getenv('PAYKEEPER_USER','').strip()
    pk_password=os.getenv('PAYKEEPER_PASSWORD','')
    pk_secret=os.getenv('PAYKEEPER_SECRET','')
    # Business constants: the bathhouse sells services at a single tax code and full payment.
    tax=os.getenv('PAYKEEPER_TAX','none').strip() or 'none'
    types=tuple(os.getenv('PAYKEEPER_'+kind+'_TYPE','full').strip() or 'full' for kind in ('DEPOSIT','FULL','BALANCE'))
    if enabled:
        parsed=urlparse(pk_url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or parsed.port not in (None,443):
            raise ValueError('PAYKEEPER_URL must be an HTTPS origin')
        if not all((web_url,pk_user,pk_password,pk_secret)) or len(pk_secret)<16:
            raise ValueError('Configure PayKeeper credentials, secret and WEB_BASE_URL')
        if tax not in ('none','vat0','vat5','vat7','vat10','vat20') or any(t not in ('prepay','part_prepay','advance','full') for t in types):
            raise ValueError('Explicitly configure approved PayKeeper receipt tax and payment types')
    return Settings(os.getenv('BOT_TOKEN', '').strip(), ids,
                    os.getenv('DATABASE_URL', '').strip(),
                    content, rules, hashlib.sha256(rules.encode()).hexdigest(), web_url,
                    enabled,pk_url,pk_user,pk_password,pk_secret,tax,*types,
                    paykeeper_settlement_enabled=os.getenv('PAYKEEPER_SETTLEMENT_ENABLED','0')=='1',
                    paykeeper_settlement_quiet=os.getenv('PAYKEEPER_SETTLEMENT_QUIET','1')=='1')
