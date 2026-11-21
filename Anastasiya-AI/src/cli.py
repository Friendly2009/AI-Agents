"""Command-line modes and Telegram transport lifetime."""
from __future__ import annotations
import argparse
import json
import logging
import os
from pathlib import Path
from delivery import GuardedTelegramClient as TelegramClient, Ledger
from campaigns import compose_first_message
from verified_leads import import_verified_csv, source_breakdown, format_pipeline
from city_sources import init_catalog
from autojoin import run_auto_join, status_report as join_status_report
from app_settings import safe_test_mode, session_path, settings

BASE = Path(__file__).resolve().parents[1]


class WrongTelegramAccount(Exception):
    pass


async def qr_login(client, display, password):
    from telethon.errors import SessionPasswordNeededError
    await client.connect()
    if await client.is_user_authorized():
        return
    while True:
        token = await client.qr_login()
        display(token.url)
        try:
            await token.wait()
        except TimeoutError:
            continue
        except SessionPasswordNeededError:
            await client.sign_in(password=await password())
        return


async def main(app, *, argv=None, login=None, connected=None, authorize_only=False, unattended=False, display_qr=None):
    parser = argparse.ArgumentParser(description='Soft Skills Telegram Outreach')
    parser.add_argument('mode', choices=['listen','send-optins','send-radar','discover','retry-crm','status','preview','radar-preview','import-verified','lead-report','join-status','join-cycle'])
    parser.add_argument('--file', default='', help='CSV с документированными согласиями для import-verified')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
    if args.mode == 'import-verified':
        if not args.file:
            raise SystemExit('Укажите --file путь_к_csv')
        stats = import_verified_csv(args.file, app.RADAR)
        print('Результат импорта:', json.dumps(stats, ensure_ascii=False, indent=2))
        print(format_pipeline(source_breakdown(app.RADAR)))
        return
    if args.mode == 'join-status':
        print(join_status_report())
        return
    if args.mode == 'lead-report':
        print(format_pipeline(source_breakdown(app.RADAR)))
        return
    if args.mode == 'preview':
        camp = app.load_active()
        print('Идея:', camp['title'], '| пауза:', camp.get('paused', True))
        print(compose_first_message('Ирина', camp))
        return
    if args.mode == 'status':
        print(f'Разрешённых первых обращений отправлено сегодня: {app.STORE.sent_today()}')
        print(f'Заявок, ожидающих отправки в CRM: {len(app.STORE.list_ready())}')
        print('FAMILY RADAR:', json.dumps(app.RADAR.summary(), ensure_ascii=False))
        print('CITY RADAR: public community sources=', len(init_catalog()))
        return
    if args.mode == 'radar-preview':
        campaign = app.load_active()
        print('Кампания:',campaign['title'],'| пауза:',campaign.get('paused',True))
        for rec in app.RADAR.candidates():
            eligible, reason = app.RADAR.can_send(rec,campaign)
            print('Кандидат:',rec['user_id'],'| можно связаться:',eligible,'|',reason)
        print('Результаты:',json.dumps(app.RADAR.summary(),ensure_ascii=False))
        return
    if safe_test_mode() and args.mode not in {'listen'}:
        raise SystemExit('SAFE_TEST_MODE=true: для безопасности доступен только режим listen (и офлайн-отчёты).')
    import sqlite3
    process_lock=sqlite3.connect(str(BASE/'data'/'running.lock.db'),timeout=0)
    try:
        process_lock.execute('BEGIN EXCLUSIVE')
    except sqlite3.OperationalError:
        process_lock.close()
        raise SystemExit('Анастасия уже запущена из этой папки. Закройте первое окно.')
    client = None
    try:
        Ledger(app.STORE).recover()
        aid, ahash = settings()
        client = TelegramClient(str(session_path()), aid, ahash, flood_sleep_threshold=0,
                                request_retries=0, auto_reconnect=(args.mode == 'listen'),
                                connection_retries=5, retry_delay=2, raise_last_call_error=True)
        if not (unattended or authorize_only):
            client.configure_delivery(app.STORE, app.director_admin_ids(0), app.M)
        if unattended:
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram session requires interactive authorization")
        elif display_qr is not None:
            await qr_login(client, display_qr, login['password'])
        else:
            await client.start(phone=os.getenv('TG_PHONE', '').strip() or None, **(login or {}))
        if authorize_only or unattended:
            account = await client.get_me()
            if getattr(account, 'phone', None) != os.environ['TG_PHONE'].lstrip('+'):
                if authorize_only:
                    await client.log_out()
                raise WrongTelegramAccount()
        if authorize_only:
            return
        client.configure_delivery(app.STORE, app.director_admin_ids(0) | {(await client.get_me()).id}, app.M)
        if args.mode == 'listen':
            if connected is not None:
                connected()
            await app.listen(client)
        elif args.mode == 'send-optins':
            await app.send_optins(client)
        elif args.mode == 'send-radar':
            await app.send_radar(client)
        elif args.mode == 'discover':
            await app.discover(client)
        elif args.mode == 'join-cycle':
            result = await run_auto_join(client)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print(join_status_report())
        elif args.mode == 'retry-crm':
            await app.retry_crm(client)
    finally:
        try:
            if client is not None:
                await client.disconnect()
        finally:
            process_lock.close()


