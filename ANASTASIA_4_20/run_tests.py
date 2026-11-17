"""Run tests in a disposable copy; never load live .env, DBs or sessions."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import ast


def test_environment(root, inherited=None):
    """Remove application configuration, including credentials, before imports."""
    env = dict(os.environ if inherited is None else inherited)
    names = {'DAILY_LIMIT', 'DRY_RUN', 'OUTREACH_ENABLED', 'OUTREACH_POLL_MINUTES'}
    for path in root.glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.isupper() and '_' in node.value:
                    names.add(node.value)
    prefixes = ('TG_', 'OPENAI_', 'ALFACRM_', 'ADMIN_', 'ANASTASIA_', 'API_',
                'AUTO_', 'FAMILY_', 'DIRECTOR_', 'VOICE_', 'AGENT_', 'PYTEST_')
    for key in list(env):
        if key in names or key.startswith(prefixes):
            env.pop(key)
    env.update(SAFE_TEST_MODE='true', CRM_ENABLED='false',
               OPENAI_API_KEY='offline-test-placeholder', PYTHONUTF8='1',
               PYTHON_DOTENV_DISABLED='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    return env


def main():
    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix='anastasia-tests-') as directory:
        target = Path(directory)
        for item in root.iterdir():
            if item.is_file() and not item.name.startswith('.') and item.suffix in {'.py', '.json', '.md', '.txt'}:
                shutil.copy2(item, target / item.name)
        shutil.copytree(root / 'tests', target / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
        env = test_environment(root)
        return subprocess.call([sys.executable, '-m', 'pytest', '-q',
                                '--basetemp', str(target / 'pytest-tmp'),
                                *sys.argv[1:]], cwd=target, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
