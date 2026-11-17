from pathlib import Path
import pytest
from doctor import configuration_errors
from run_tests import test_environment as isolated_environment
from release_gate import blockers, CRITICAL, QUALITY, INTEGRATIONS


def valid_config():
    return dict(TG_API_ID='123', TG_API_HASH='a'*32, OPENAI_API_KEY='synthetic',
                ADMIN_OWNER_TG_ID='101', ADMIN_WIFE_TG_ID='202')


@pytest.mark.parametrize('key,value', [
    ('TG_API_ID','0'), ('TG_API_ID','abc'), ('TG_API_ID','١٢٣'),
    ('TG_API_HASH','short'), ('OPENAI_API_KEY',' '), ('OPENAI_MODEL',''),
    ('ADMIN_WIFE_TG_ID','101'), ('ADMIN_OWNER_TG_ID','-1'),
    ('TG_SESSION_NAME','../production'), ('SAFE_TEST_MODE','flase'),
    ('CRM_ENABLED','yes')])
def test_invalid_config_blocks(key, value):
    env = valid_config(); env[key] = value
    assert configuration_errors(env)


def test_valid_config_is_only_syntactic():
    assert not configuration_errors(valid_config())


def test_runner_strips_inherited_credentials():
    env = isolated_environment(Path(__file__).resolve().parents[1], {
        'PATH':'keep', 'TG_PHONE':'secret', 'ALFACRM_ALEX_API_KEY':'secret',
        'ADMIN_OWNER_TG_ID':'secret', 'OPENAI_BASE_URL':'https://production',
        'SAFE_TEST_MODE':'false', 'DAILY_LIMIT':'999', 'PYTEST_ADDOPTS':'--bad'})
    assert 'secret' not in env.values()
    assert 'OPENAI_BASE_URL' not in env and 'DAILY_LIMIT' not in env
    assert env['PATH']=='keep' and env['SAFE_TEST_MODE']=='true'
    assert env['PYTHON_DOTENV_DISABLED']=='1'


def evidence():
    return dict(automated_tests='passed', integration=dict.fromkeys(INTEGRATIONS,'passed'),
                cases={'A01':dict(critical=dict.fromkeys(CRITICAL,True),
                                 scores=dict.fromkeys(QUALITY,5), evidence='synthetic test fixture')})


@pytest.mark.parametrize('check', CRITICAL)
def test_critical_failure_overrides_perfect_scores(check):
    data=evidence(); data['cases']['A01']['critical'][check]=False
    assert blockers(data,['A01'])


@pytest.mark.parametrize('name', INTEGRATIONS)
def test_unverified_integrations_block(name):
    data=evidence(); data['integration'][name]='not_run'
    assert blockers(data,['A01'])


def test_absent_evidence_and_missing_cases_block():
    assert blockers({},['A01'])
    assert blockers(evidence(),['A01','A02'])
    assert not blockers(evidence(),['A01'])


@pytest.mark.parametrize('score', [None, True, 0, 3, 6, float('nan')])
def test_invalid_or_low_score_blocks(score):
    data=evidence(); data['cases']['A01']['scores']['accuracy']=score
    assert blockers(data,['A01'])
