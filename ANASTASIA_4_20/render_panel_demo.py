"""Reproducible offline UI evidence using the real WSGI app and temporary databases."""
import io
import os
from pathlib import Path
import re
import tempfile
from contextlib import ExitStack
from urllib.parse import urlencode
from unittest.mock import patch


def render():
    # No actual user configuration, identities, credentials or data are used.
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ADMIN_OWNER_TG_ID': '101', 'ADMIN_WIFE_TG_ID': '202', 'PATH': os.environ.get('PATH', '')}, clear=True), ExitStack() as cleanup:
        from admin_panel import Panel
        from storage import Store
        db = str(Path(directory) / 'demo.db')
        store = Store(db)
        cleanup.callback(store.db.close)
        store.ensure(4242)
        store.update(4242, name='Демонстрационный клиент', city='Александров', wants_trial=1)
        cfg = store.configuration()
        base = dict(settings={'conversation_style': 'friendly', 'warmth': 4}, prompt='Объясняй учебные проекты понятными примерами.', enabled=True, deleted=False, group='')
        cfg.save(101, 'global', 'global', base, 0)
        group = dict(base, settings={'conversation_style': 'detailed'}, prompt='Используй технологические примеры без лишней терминологии.')
        cfg.save(202, 'group', 'Интересуются программированием', group, 0)
        cfg.save(101, 'client', '4242', dict(base, settings={'verbosity': 1}, group='Интересуются программированием'), 0)
        app = Panel(db, manager_path=str(Path(directory) / 'manager.db'))
        cookie = ''
        def request(data=None):
            nonlocal cookie
            raw = urlencode(data or {}).encode()
            env = {'REMOTE_ADDR': '127.0.0.1', 'HTTP_HOST': '127.0.0.1:8765', 'PATH_INFO': '/', 'REQUEST_METHOD': 'POST' if data is not None else 'GET', 'HTTP_COOKIE': cookie, 'HTTP_ORIGIN': 'http://127.0.0.1:8765', 'CONTENT_TYPE': 'application/x-www-form-urlencoded', 'CONTENT_LENGTH': str(len(raw)), 'wsgi.input': io.BytesIO(raw)}
            captured = {}
            def start(status, headers):
                captured.update(status=status, headers=headers)
            body = b''.join(app(env, start)).decode('utf-8')
            assert captured['status'] == '200 OK', captured['status']
            for key, value in captured['headers']:
                if key == 'Set-Cookie':
                    cookie = value.split(';')[0]
            return body, re.search(r'name="csrf" value="([^"]+)"', body)[1]
        _, csrf = request()
        body, csrf = request(dict(action='login', csrf=csrf, code=cfg.issue_login(101)))
        out = Path(__file__).resolve().parent / 'evidence'
        out.mkdir(exist_ok=True)
        def save(name, content):
            content = re.sub(r'name="csrf" value="[^"]+"', 'name="csrf" value="DEMO-NOT-A-SESSION"', content)
            content = content.replace('<form ', '<form inert ').replace('<button>', '<button disabled>')
            content = content.replace('<h1>', '<p class="notice">Статический офлайн-пример · вымышленные данные · формы отключены</p><h1>', 1)
            (out / name).write_text(content, encoding='utf-8')
        save('admin-panel.html', body)
        preview, _ = request(dict(action='preview', csrf=csrf, scope='client', target='4242', dialogue='[{"role":"user","content":"Какие проекты создают на программировании?"}]'))
        save('prompt-preview.html', preview)
        store.db.close()
        print('Created evidence/admin-panel.html and evidence/prompt-preview.html')


if __name__ == '__main__':
    render()
