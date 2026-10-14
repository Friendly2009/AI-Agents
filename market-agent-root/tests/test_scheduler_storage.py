import asyncio
import json
from datetime import date, datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from market_agent.api import create_app
from market_agent.config import Instrument
from market_agent.models import Profile
from market_agent.network import HTTP, SourceError
from market_agent.providers import MOEX
from market_agent.recommendations import POLICIES, allocate, request_signals
from market_agent.service import Service
from market_agent.storage import Store

NOW = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)
END = date(2026, 10, 13)


def profile(**kwargs):
    return Profile(cash_rub=kwargs.pop('cash_rub', 100000), horizon_days=30,
                   risk=kwargs.pop('risk', 'balanced'), **kwargs)


def entry(symbol='SBER', action='buy', lot=10, price=100):
    return {'key': f'MOEX:TQBR:{symbol}', 'instrument': {'symbol': symbol},
            'analysis_kind': 'ai', 'warnings': [], 'security': {'lot_size': lot},
            'metrics': {'last_date': END.isoformat(), 'last_close': price, 'observations': 100,
                        'atr14': price * .02, 'sma20': price * .98, 'sma60': price * .95,
                        'average_turnover20': 5000000, 'weekly_change_pct': 2},
            'news': [{'id': 'news1'}], 'ai': {'action': action, 'sell_fraction': 'all'}}


@pytest.mark.parametrize('risk', list(POLICIES))
@pytest.mark.parametrize('cash', [1, 1000, 100000, 10000000])
def test_shared_budget_lots_and_risk_caps(risk, cash):
    rows = [entry(f'S{i}', lot=1 if i % 2 else 10, price=100 + i * 10) for i in range(20)]
    p = profile(cash_rub=cash, risk=risk)
    result = allocate(rows, p, END, NOW)
    rule = POLICIES[risk]
    assert sum(e['recommendation']['estimated_cash_rub'] for e in rows) <= cash * rule['deploy_cash_pct'] / 100 + .1
    assert result['new_positions_risk_rub'] <= cash * rule['total_risk_pct'] / 100 + .01
    for e in rows:
        r = e['recommendation']
        assert r['shares'] % e['security']['lot_size'] == 0
        assert r['estimated_cash_rub'] <= cash * rule['position_pct'] / 100 + .01
        assert r['estimated_risk_rub'] <= cash * rule['idea_risk_pct'] / 100 + .01
        assert r['scenario_only']
    assert result['cash_after_buys_rub'] >= 0


def test_known_empty_portfolio_and_holdings_exposure():
    row = entry()
    p = profile(portfolio_known=True, holdings=[{'symbol': 'SBER', 'shares': 1000}])
    allocate([row], p, END, NOW)
    assert row['recommendation']['action'] == 'wait'  # Already exceeds position cap.
    row = entry()
    allocate([row], profile(portfolio_known=True), END, NOW)
    assert row['recommendation']['action'] == 'buy'
    assert not row['recommendation']['scenario_only']


@pytest.mark.parametrize('known,holdings,expected', [
    (False, [], 0), (True, [], 0), (True, [{'symbol': 'SBER', 'shares': 37}], 30),
])
def test_no_short_and_sell_only_whole_owned_lots(known, holdings, expected):
    row = entry(action='sell')
    allocate([row], profile(portfolio_known=known, holdings=holdings), END, NOW)
    assert row['recommendation']['shares'] == expected


def test_sale_proceeds_not_spent_and_fraction_is_respected():
    rows = [entry(action='sell'), entry('GAZP')]
    rows[0]['ai']['sell_fraction'] = 'half'
    p = profile(cash_rub=0, portfolio_known=True, holdings=[{'symbol': 'SBER', 'shares': 100}])
    result = allocate(rows, p, END, NOW)
    assert rows[0]['recommendation']['shares'] == 50
    assert rows[1]['recommendation']['action'] == 'wait'
    assert result['planned_buys_rub'] == 0


@pytest.mark.parametrize('bad', ['stale', 'missing_lot', 'no_news', 'no_ai', 'low_liquidity', 'short_history', 'downtrend'])
def test_data_and_signal_gates(bad):
    row = entry()
    if bad == 'stale':
        row['metrics']['last_date'] = '2026-09-01'
    elif bad == 'missing_lot':
        row.pop('security')
    elif bad == 'no_news':
        row['news'] = []
    elif bad == 'no_ai':
        row['analysis_kind'] = 'calculated'
    elif bad == 'low_liquidity':
        row['metrics']['average_turnover20'] = 100
    elif bad == 'short_history':
        row['metrics']['observations'] = 10
    elif bad == 'downtrend':
        row['metrics']['sma20'] = 200
    allocate([row], profile(), END, NOW)
    assert row['recommendation']['action'] == 'wait'
    assert row['recommendation']['shares'] == 0
    assert row['recommendation']['reasons']


def test_unvalued_holding_blocks_all_buys():
    row = entry()
    result = allocate([row], profile(portfolio_known=True, holdings=[{'symbol': 'GAZP', 'shares': 20}]), END, NOW)
    assert result['estimated_equity_rub'] is None
    assert row['recommendation']['action'] == 'wait'


@pytest.mark.parametrize('changes', [
    {'cash_rub': float('nan')}, {'cash_rub': -1}, {'cash_rub': 0},
    {'portfolio_known': False, 'holdings': [{'symbol': 'SBER', 'shares': 1}]},
    {'portfolio_known': True, 'holdings': [{'symbol': 'SBER', 'shares': -1}]},
    {'portfolio_known': True, 'holdings': [{'symbol': 'SBER', 'shares': 1.5}]},
    {'portfolio_known': True, 'holdings': [{'symbol': 'SBER', 'shares': 1}] * 2},
])
def test_profile_validation(changes):
    with pytest.raises(ValidationError):
        profile(**changes)


def test_profile_api_auth_persistence_and_unknown_symbols(settings):
    app = create_app(settings)
    auth = {'Authorization': 'Bearer ' + settings.api_token.get_secret_value()}
    with TestClient(app) as c:
        assert c.get('/api/profile').status_code == 401
        assert c.post('/api/profile', json=profile().model_dump()).status_code == 401
        assert c.get('/api/profile', headers=auth).json()['saved'] is None
        body = profile(portfolio_known=True).model_dump()
        assert c.post('/api/profile', headers=auth, json=body).status_code == 200
        assert Store(settings.database_path).profile()['profile'] == body
        body['holdings'] = [{'symbol': 'UNKNOWN', 'shares': 1}]
        assert c.post('/api/profile', headers=auth, json=body).status_code == 422
        body['cash_rub'] = -100
        assert c.post('/api/profile', headers=auth, json=body).status_code == 422
        assert c.get('/api/profile', headers=auth).json()['saved']['profile']['cash_rub'] == 100000


@pytest.mark.parametrize('defect', ['none', 'quantity', 'source', 'ticker', 'duplicate', 'no_evidence', 'truncated'])
def test_ai_output_is_validated(settings, defect):
    async def run():
        signal = {'symbol': 'SBER', 'action': 'buy', 'sell_fraction': 'all',
                  'summary': 'Рост [news1]', 'risks': ['Риск'], 'source_ids': ['news1']}
        if defect == 'quantity':
            signal['shares'] = 10000000
        if defect == 'source':
            signal['source_ids'] = ['invented']
        if defect == 'ticker':
            signal['symbol'] = 'UNKNOWN'
        if defect == 'no_evidence':
            signal['source_ids'] = []
        signals = [signal, signal] if defect == 'duplicate' else [signal]
        response = {'choices': [{'finish_reason': 'length' if defect == 'truncated' else 'stop',
                                'message': {'content': json.dumps({'signals': signals})}}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))) as c:
            if defect == 'none':
                assert (await request_signals(HTTP(c), settings, profile(), [entry()]))['SBER']['action'] == 'buy'
            else:
                with pytest.raises(SourceError):
                    await request_signals(HTTP(c), settings, profile(), [entry()])
    asyncio.run(run())


@pytest.mark.parametrize('lot,currency,ok', [(10, 'SUR', True), (0, 'SUR', False), (1.5, 'SUR', False), (10, 'USD', False)])
def test_metadata_lots_and_currency(lot, currency, ok):
    async def run():
        body = {'securities': {'columns': ['SECID', 'LOTSIZE', 'CURRENCYID'], 'data': [['SBER', lot, currency]]}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as c:
            if ok:
                assert (await MOEX(HTTP(c)).security(Instrument(symbol='SBER')))['lot_size'] == lot
            else:
                with pytest.raises(SourceError):
                    await MOEX(HTTP(c)).security(Instrument(symbol='SBER'))
    asyncio.run(run())


def test_503_retries_bounded_and_does_not_leak_key(monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr('market_agent.network.asyncio.sleep', no_sleep)
    async def run():
        calls = []
        def respond(r):
            calls.append(r)
            return httpx.Response(503, json={'error': {'message': 'high demand SECRET_KEY'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as c:
            with pytest.raises(SourceError) as e:
                await HTTP(c).json('POST', 'https://example.com', retry_503=2)
            assert len(calls) == 3 and 'перегружена' in str(e.value) and 'SECRET_KEY' not in str(e.value)
    asyncio.run(run())


def test_profile_report_uses_single_batch_and_freezes_snapshot(settings):
    async def run():
        from market_agent.models import News
        from market_agent.providers import Demo
        class Provider(Demo):
            async def security(self, instrument):
                return {'lot_size': 1, 'currency': 'RUB'}
        store = Store(settings.database_path)
        store.save_profile(profile(portfolio_known=True))
        settings.mode = 'live'
        settings.llm_enabled = True
        markets = settings.markets()
        markets.news_feeds = ['https://example.com/rss']
        markets.instruments.append(Instrument(symbol='GAZP'))
        for symbol in ['SBER', 'GAZP']:
            news = News(id=symbol, title=symbol, url='https://example.com/'+symbol, published_at=NOW,
                        feed_url=markets.news_feeds[0])
            store.cache_news([news], markets.news_feeds, NOW, [])
        calls = []
        def respond(r):
            calls.append(r)
            data = json.loads(json.loads(r.content)['messages'][1]['content'])
            assert data['profile']['cash_rub'] == 100000
            # Concurrent profile change must not affect this report.
            store.save_profile(profile(cash_rub=200000))
            signals = [{'symbol': e['symbol'], 'action': 'buy', 'sell_fraction': 'all',
                        'summary': 'Тренд подтверждён', 'risks': ['Риск'],
                        'source_ids': [e['news'][0]['id']]} for e in data['instruments']]
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                'message': {'content': json.dumps({'signals': signals})}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            service = Service(settings, markets, store, HTTP(client))
            service.provider = Provider()
            result = await service.build_report(NOW)
        assert len(calls) == 1
        assert result['profile']['profile']['cash_rub'] == 100000
        assert result['allocation']['cash_rub'] == 100000
        assert result['ai_complete']
        assert result['allocation']['planned_buys_rub'] > 0
        assert all(e['recommendation']['action'] == 'buy' for e in result['instruments'])
        run_id = store.claim()
        store.finish(run_id, report=result)
        assert store.get(run_id)['report']['profile']['profile']['cash_rub'] == 100000
    asyncio.run(run())
