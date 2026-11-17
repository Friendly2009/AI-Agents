"""Loopback-only administration. No new web dependencies or secret copies."""
import argparse
import asyncio
import html
import json
import os
from pathlib import Path
import secrets
import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode
from wsgiref.simple_server import make_server, WSGIRequestHandler

from dotenv import load_dotenv
from agent_config import CHOICES, SCALES, Conflict, defaults, languages, require_admin
from storage import Store

ROOT = Path(__file__).resolve().parent


def esc(value):
    return html.escape(str(value), quote=True)


def dump(value):
    return '<pre>' + esc(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre>'


def field(name, value='', label=None, kind='text'):
    return f'<label>{esc(label or name)}<input name="{esc(name)}" type="{kind}" value="{esc(value)}" required></label>'


def hidden(name, value):
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


def select(name, choices, value, label=None):
    return '<label>' + esc(label or name) + f'<select name="{esc(name)}">' + ''.join(
        f'<option value="{esc(x)}"' + (' selected' if x == value else '') + f'>{esc(x or "Наследовать")}</option>' for x in choices) + '</select></label>'


def page(body):
    return '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Анастасия · Настройки</title><style>
:root{font:16px/1.55 system-ui;color:#192f38;background:#f3f6f7}body{max-width:1100px;margin:32px auto;padding:0 24px}
h1{font-size:30px;margin-bottom:4px}h2{font-size:21px}nav{display:flex;gap:20px;flex-wrap:wrap;margin:24px 0}
a{color:#176b65}section{background:white;border:1px solid #dce5e7;border-radius:14px;padding:24px;margin:20px 0}
label{display:block;color:#3c555e;font-size:14px}input,select,textarea{display:block;box-sizing:border-box;width:100%;padding:10px;border:1px solid #b6c8cc;border-radius:7px;background:#fff;color:#192f38;font:inherit;margin:5px 0 14px}
textarea{min-height:150px}button{padding:11px 18px;border:0;border-radius:7px;background:#176b65;color:white;font:inherit;margin:8px 8px 8px 0;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px 24px}.muted{color:#62777e}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f6f7;padding:16px;border-radius:8px;font-size:13px}table{width:100%;border-collapse:collapse}td,th{text-align:left;border-bottom:1px solid #e2e9eb;padding:9px}.notice{padding:16px;background:#e2f1ed;border-radius:9px}
</style><h1>Анастасия Владимировна</h1><p class="muted">Управление консультантом · локальная панель</p>''' + body + '</html>'


def service_status():
    # Presence is not health. Never return secret values or exception messages.
    return {
        'OpenAI': {'configured': bool(os.getenv('OPENAI_API_KEY')), 'connection': 'не проверено; проверка — тестовый ответ'},
        'Telegram': {'configured': bool(os.getenv('TG_API_ID') and os.getenv('TG_API_HASH')), 'connection': 'неизвестно; панель не подключает Telegram'},
        'AlfaCRM': {'enabled': os.getenv('CRM_ENABLED', 'false').lower() == 'true',
                    'Александров': bool(os.getenv('ALFACRM_ALEX_URL') and os.getenv('ALFACRM_ALEX_EMAIL') and os.getenv('ALFACRM_ALEX_API_KEY')),
                    'Ярославль': bool(os.getenv('ALFACRM_YARO_URL') and os.getenv('ALFACRM_YARO_EMAIL') and os.getenv('ALFACRM_YARO_API_KEY')),
                    'connection': 'не проверено; реальные заявки из панели не создаются'},
        'SAFE_TEST_MODE': os.getenv('SAFE_TEST_MODE', 'true').lower() != 'false',
    }


class Panel:
    def __init__(self, db_path=None, port=8765, manager_path=None):
        self.db_path = db_path
        self.manager_path = manager_path
        self.origin = f'http://127.0.0.1:{port}'
        self.host = f'127.0.0.1:{port}'
        self.sessions = {}
        self.attempts = []

    def form(self, csrf, action, body):
        return '<form method="post" action="/">' + hidden('csrf', csrf) + hidden('action', action) + body + '</form>'

    def __call__(self, env, start_response):
        store = None
        status, extra = '200 OK', []
        try:
            if env.get('REMOTE_ADDR') not in ('127.0.0.1', '::1') or env.get('HTTP_HOST') != self.host:
                raise PermissionError('Разрешён только локальный адрес 127.0.0.1')
            if env.get('PATH_INFO') != '/':
                raise ValueError('Страница не найдена')
            method = env.get('REQUEST_METHOD', 'GET')
            if method not in ('GET', 'POST'):
                raise ValueError('Метод не поддерживается')
            cookies = SimpleCookie()
            cookies.load(env.get('HTTP_COOKIE', ''))
            sid = cookies['anastasia_admin'].value if 'anastasia_admin' in cookies else ''
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > now}
            session = self.sessions.get(sid)
            if session and session['actor'] is not None:
                require_admin(session['actor'])  # Revoked Telegram IDs lose access immediately.
            data = {k: v[0] for k, v in parse_qs(env.get('QUERY_STRING', ''), keep_blank_values=True, max_num_fields=60).items()}
            if method == 'POST':
                if env.get('HTTP_ORIGIN') != self.origin:
                    raise PermissionError('CSRF: неверный Origin')
                if env.get('CONTENT_TYPE', '').split(';')[0] != 'application/x-www-form-urlencoded':
                    raise ValueError('Неподдерживаемый формат')
                size = int(env.get('CONTENT_LENGTH') or '0')
                if not 0 < size <= 40000:
                    raise ValueError('Слишком большой запрос')
                data = {k: v[0] for k, v in parse_qs(env['wsgi.input'].read(size).decode('utf-8'), keep_blank_values=True, max_num_fields=60).items()}
                if not session or not secrets.compare_digest(data.get('csrf', ''), session['csrf']):
                    raise PermissionError('CSRF: обновите страницу')
            if not session:
                sid = secrets.token_urlsafe(32)
                session = dict(actor=None, csrf=secrets.token_urlsafe(32), expires=now + 1800)
                self.sessions[sid] = session
                extra.append(('Set-Cookie', f'anastasia_admin={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age=1800'))
            store = Store(self.db_path, legacy_manager_path=self.manager_path)
            cfg = store.configuration()
            action = data.get('action', '') if method == 'POST' else ''
            if action == 'login':
                self.attempts = [t for t in self.attempts if now - t < 60]
                if len(self.attempts) >= 10:
                    raise PermissionError('Слишком много попыток; подождите минуту')
                self.attempts.append(now)
                actor = cfg.consume_login(data.get('code', ''))
                self.sessions.pop(sid, None)
                sid = secrets.token_urlsafe(32)
                session = dict(actor=actor, csrf=secrets.token_urlsafe(32), expires=now + 1800)
                self.sessions[sid] = session
                extra.append(('Set-Cookie', f'anastasia_admin={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age=1800'))
                action = ''
            if session['actor'] is None:
                if action:
                    raise PermissionError('Требуется вход администратора')
                body = '<section><h2>Вход администратора</h2><p>Получите одноразовый код командой /панель в личном диалоге с агентом или в локальной консоли.</p>'
                body += self.form(session['csrf'], 'login', field('code', label='Одноразовый код', kind='password') + '<button>Войти</button>') + '</section>'
            elif action == 'logout':
                self.sessions.pop(sid, None)
                extra.append(('Set-Cookie', 'anastasia_admin=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'))
                body = '<section>Вы вышли. <a href="/">Войти снова</a></section>'
            else:
                require_admin(session['actor'])
                body = self.dashboard(store, cfg, session, data, action)
        except PermissionError as exc:
            status, body = '403 Forbidden', '<section>' + esc(exc) + '<p><a href="/">Вернуться</a></p></section>'
        except Conflict as exc:
            status, body = '409 Conflict', '<section>' + esc(exc) + '<p><a href="/">Обновить</a></p></section>'
        except (ValueError, KeyError, TypeError):
            status, body = '400 Bad Request', '<section>Некорректные данные. Проверьте значения и версию. <a href="/">Вернуться</a></section>'
        except Exception:
            # SDK/DB errors may contain credentials or personal data. Never echo them.
            status, body = '500 Internal Server Error', '<section>Операция не выполнена. Проверьте локальную конфигурацию и доступность сервиса.</section>'
        finally:
            if store:
                store.db.close()
        raw = page(body).encode('utf-8')
        start_response(status, [('Content-Type', 'text/html; charset=utf-8'), ('Content-Length', str(len(raw))),
                                ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                                ('Referrer-Policy', 'no-referrer'), ('X-Frame-Options', 'DENY'),
                                ('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")] + extra)
        return [raw]

    def dashboard(self, store, cfg, session, data, action):
        actor, csrf = session['actor'], session['csrf']
        scope, target = data.get('scope', 'global'), data.get('target', 'global')
        cfg.key(scope, target)
        note = ''
        if action in ('save', 'disable', 'enable', 'delete', 'rollback'):
            expected = int(data['revision'])
            if action == 'rollback':
                cfg.rollback(actor, scope, target, int(data['version']), expected)
            else:
                item = cfg.read(scope, target)
                item.pop('revision')
                if action == 'save':
                    settings = {}
                    for key in defaults():
                        value = data.get('setting_' + key, '')
                        if not value:
                            continue
                        settings[key] = int(value) if key in SCALES else (value == 'true' if key == 'outbound_allowed' else value)
                    item = dict(settings=settings, prompt=data.get('prompt', ''), group=data.get('group', ''), enabled=True, deleted=False)
                elif action == 'delete':
                    item = dict(settings={}, prompt='', group='', enabled=False, deleted=True)
                else:
                    item['enabled'] = action == 'enable'
                cfg.save(actor, scope, target, item, expected, action)
            note = '<p class="notice">Сохранено. Новая версия применяется к следующим сообщениям.</p>'
        if action == 'slot':
            from manager import Manager
            manager = Manager(self.manager_path).bind(store)
            try:
                manager.add_slot(data['city'], data['course'][:150], data['starts'], int(data['capacity']))
                self.audit(store, actor, 'slot-created')
            finally:
                manager.db.close()
            note = '<p class="notice">Занятие добавлено.</p>'
        if action == 'handoff':
            from manager import Manager
            uid = int(data['uid'])
            if not store.get(uid):
                raise ValueError('Клиент не найден')
            manager = Manager(self.manager_path).bind(store)
            try:
                manager.handoff(uid)
                self.audit(store, actor, 'handoff-queued', str(uid))
            finally:
                manager.db.close()
            note = '<p class="notice">Диалог приостановлен и помещён в очередь сотрудника. Уведомление не отправлялось.</p>'
        if action == 'stop':
            uid = int(data['uid'])
            if not store.get(uid):
                raise ValueError('Клиент не найден')
            ConversationState(store).update(uid, stopped=1)
            self.audit(store, actor, 'contact-stopped', str(uid))
            note = '<p class="notice">Дальнейшее общение запрещено.</p>'
        item = cfg.read(scope, target)
        common = hidden('scope', scope) + hidden('target', target) + hidden('revision', item['revision'])
        resolved = cfg.resolve(int(target) if scope == 'client' else None, target if scope == 'group' else None)
        nav = '<nav><a href="#general">Общие</a><a href="#character">Характер</a><a href="#prompts">Промпты</a><a href="#clients">Клиенты</a><a href="#integrations">Интеграции</a><a href="#testing">Тестирование</a></nav>'
        body = nav + note + f'<p class="muted">Администратор {actor} · версия {item["revision"]} · ' + esc(scope + ' / ' + target) + '</p>'
        body += self.form(csrf, 'logout', '<button>Выйти</button>')
        body += '<section><h2>Область настроек</h2><form method="get"><div class="grid">' + select('scope', ('global', 'group', 'client'), scope, 'Область') + field('target', target, 'global / название группы / Telegram ID клиента') + '</div><button>Открыть или создать</button></form><p class="muted">Пустое поле наследует значение. У клиента одна группа; персональные значения имеют приоритет.</p></section>'
        body += '<section><h2>Сохранённые области</h2>' + ''.join('<p><a href="/?' + esc(urlencode(dict(scope=r[0], target=r[1]))) + '">' + esc(r[0] + ' / ' + r[1]) + '</a></p>' for r in store.db.execute('SELECT scope,target FROM agent_config ORDER BY scope,target LIMIT 200')) + '</section>'
        controls = '<section id="general"><h2>Общие</h2><div class="grid">'
        values = item['settings']
        controls += field_optional('setting_agent_display_name', values.get('agent_display_name', ''), 'Имя консультанта')
        controls += select('setting_response_language', ('',) + languages(), values.get('response_language', ''), 'Язык')
        if scope == 'global':
            controls += field_optional('setting_model', values.get('model', ''), 'Модель LLM')
            controls += select('setting_mode', ('', 'active', 'paused'), values.get('mode', ''), 'Режим работы')
        controls += select('setting_outbound_allowed', ('', 'true', 'false'), str(values.get('outbound_allowed', '')).lower(), 'Разрешать допустимые исходящие')
        controls += '</div><p class="muted">Разрешение исходящих не заменяет согласия клиента и SAFE_TEST_MODE.</p></section><section id="character"><h2>Характер</h2><div class="grid">'
        for key, choices in CHOICES.items():
            controls += select('setting_' + key, ('',) + choices, values.get(key, ''), key)
        for key in SCALES:
            controls += select('setting_' + key, ('', '0', '1', '2', '3', '4', '5'), str(values.get(key, '')), key)
        controls += '</div></section><section id="prompts"><h2>Промпты</h2><p>Редактируется дополнительная манера общения. Ограничения безопасности и согласий закреплены в коде.</p>'
        if scope == 'client':
            controls += field_optional('group', item['group'], 'Группа клиента')
        controls += '<label>Дополнительные инструкции<textarea name="prompt" maxlength="6000">' + esc(item['prompt']) + '</textarea></label><button>Проверить и сохранить</button></section>'
        body += self.form(csrf, 'save', common + controls)
        body += '<section><h2>История и применение</h2><p>Состояние: ' + ('удалено' if item['deleted'] else 'включено' if item['enabled'] else 'отключено') + '</p>'
        for act, label in [('disable', 'Отключить'), ('enable', 'Включить'), ('delete', 'Удалить текущую версию')]:
            body += self.form(csrf, act, common + '<button>' + label + '</button>')
        body += dump(cfg.history(actor, scope, target))
        body += self.form(csrf, 'rollback', common + field('version', label='Номер версии для отката', kind='number') + '<button>Откатить</button>')
        body += '<details><summary>Предпросмотр: итоговые параметры и конфликты</summary>' + dump(resolved) + '</details></section>'
        body += self.clients(store, csrf, data)
        body += '<section id="integrations"><h2>Интеграции</h2>' + dump(service_status()) + '<p class="muted">Ключи и Telegram-сессии не отображаются. Обновление секретов — только в существующем .env на компьютере агента.</p></section>'
        body += '<section id="testing"><h2>Тестирование</h2><p>Выбрана текущая область. Тестовые сообщения и ответ не записываются в историю клиентов. Telegram и CRM не вызываются.</p>'
        test_form = common + '<label>Тестовый диалог: JSON-массив user/assistant<textarea name="dialogue">' + esc(data.get('dialogue', '[{"role":"user","content":"Расскажите о занятиях"}]')) + '</textarea></label>'
        body += self.form(csrf, 'preview', test_form + '<button>Предпросмотр запроса</button>')
        body += self.form(csrf, 'test', test_form + '<button>Получить ответ LLM</button>')
        if action in ('preview', 'test'):
            history = json.loads(data['dialogue'])
            if not isinstance(history, list) or not 1 <= len(history) <= 20 or any(not isinstance(t, dict) or set(t) != {'role', 'content'} or t['role'] not in ('user', 'assistant') or not isinstance(t['content'], str) or len(t['content']) > 4000 for t in history):
                raise ValueError('Некорректный диалог')
            from manager import Manager, knowledge
            from director_orders import public_context
            from trial_offer import offer_context
            from prompt_builder import build_prompt
            manager = Manager(':memory:')
            real_manager = Manager(self.manager_path).bind(store)
            try:
                manager.slots = real_manager.slots  # Preview and test use identical verified slots; usage remains in memory.
                instructions, inputs, state = build_prompt(history, facts=knowledge(), orders=public_context(), slots=real_manager.slots(), offer=offer_context(), configuration=resolved)
                body += '<h3>Техническая интерпретация</h3><p class="muted">Параметры и вычисленная политика речи; скрытые рассуждения модели не запрашиваются.</p>' + dump(state)
                body += '<details><summary>Собранный запрос</summary>' + dump(dict(instructions=instructions, input=inputs)) + '</details>'
                if action == 'test':
                    from brain import respond
                    result = asyncio.run(respond(history, manager=manager, configuration=resolved))
                    body += '<h3>Ответ LLM</h3>' + dump(result)
            finally:
                manager.db.close()
                real_manager.db.close()
        body += '</section>'
        from manager import Manager
        manager = Manager(self.manager_path).bind(store)
        try:
            body += '<section><h2>Занятия и аналитика</h2>' + dump(manager.slots()) + '<pre>' + esc(manager.report(store)) + '</pre>'
            body += self.form(csrf, 'slot', '<div class="grid">' + select('city', ('Александров', 'Ярославль'), 'Александров', 'Город') + field('course', label='Название занятия') + field('starts', label='Дата ISO, например 2027-01-20T18:00:00+03:00') + field('capacity', '10', 'Количество мест', 'number') + '</div><button>Добавить занятие</button>') + '</section>'
        finally:
            manager.db.close()
        body += '<section><h2>Аудит операций</h2>' + dump([dict(r) for r in store.db.execute('SELECT actor,action,target,at FROM panel_audit ORDER BY id DESC LIMIT 50')]) + '</section>'
        return body

    def clients(self, store, csrf, data):
        query = data.get('search', '')[:80]
        rows = store.db.execute('SELECT user_id,name,city,stopped,wants_trial,crm_id FROM contacts WHERE CAST(user_id AS TEXT) LIKE ? OR name LIKE ? ORDER BY updated_at DESC LIMIT 30', ('%' + query + '%', '%' + query + '%')).fetchall()
        result = '<section id="clients"><h2>Клиенты</h2><form method="get">' + field_optional('search', query, 'Поиск по имени или Telegram ID') + '<button>Найти</button></form><table><tr><th>Клиент</th><th>Статус</th><th>Действия</th></tr>'
        for row in rows:
            uid = row['user_id']
            item = store.configuration().read('client', str(uid))
            link = '/?' + urlencode(dict(scope='client', target=uid))
            status = 'Отказ от общения' if row['stopped'] else 'В CRM' if row['crm_id'] else 'Интерес к занятию' if row['wants_trial'] else 'Консультация'
            result += '<tr><td><a href="' + esc(link) + '">' + esc(row['name'] or uid) + '</a><br>' + esc(item['group'] or 'Без группы') + '</td><td>' + status + '</td><td>'
            for act, label in [('stop', 'Запретить общение'), ('handoff', 'В очередь сотрудника')]:
                result += self.form(csrf, act, hidden('uid', uid) + '<button>' + label + '</button>')
            result += '</td></tr>'
        return result + '</table></section>'

    @staticmethod
    def audit(store, actor, action, target=''):
        from clock_utils import utc_now
        with store.db:
            store.db.execute('INSERT INTO panel_audit(actor,action,target,at) VALUES(?,?,?,?)', (actor, action, target, utc_now()))


def field_optional(name, value, label):
    return field(name, value, label).replace(' required>', '>')


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass  # No URL, code, cookie or user content in access logs.


def main():
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description='Локальная панель Анастасии')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--login-code', type=int, metavar='ADMIN_TG_ID', help='Выдать код входа администратору из .env и завершить')
    args = parser.parse_args()
    if args.login_code is not None:
        store = Store()
        try:
            print(store.configuration().issue_login(args.login_code))
        finally:
            store.db.close()
        return
    app = Panel(port=args.port)
    with make_server('127.0.0.1', args.port, app, handler_class=QuietHandler) as server:
        print(f'Панель: {app.origin} (Ctrl+C для остановки)')
        server.serve_forever()


if __name__ == '__main__':
    main()

from conversation_service import ConversationState
