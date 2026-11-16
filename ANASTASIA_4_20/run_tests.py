"""Run tests in a disposable copy; never load live .env, DBs or sessions."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix='anastasia-tests-') as directory:
        target = Path(directory)
        for item in root.iterdir():
            if item.is_file() and not item.name.startswith('.') and item.suffix in {'.py', '.json', '.md', '.txt'}:
                shutil.copy2(item, target / item.name)
        shutil.copytree(root / 'tests', target / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
        env = dict(os.environ, SAFE_TEST_MODE='true', CRM_ENABLED='false',
                   OPENAI_API_KEY='offline-test-placeholder', PYTHONUTF8='1')
        return subprocess.call([sys.executable, '-m', 'pytest', '-q', *sys.argv[1:]], cwd=target, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
