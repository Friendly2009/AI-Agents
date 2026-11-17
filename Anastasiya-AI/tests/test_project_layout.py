"""Protect resource locations and the relocated command launcher."""
from pathlib import Path
import subprocess
import sys

from app_settings import BASE, allowed_groups
from sales_expertise import load_expertise


def test_resources_are_outside_source_directory():
    root = Path(__file__).resolve().parents[1]
    assert BASE == root
    assert (root / 'resources' / 'knowledge.json').is_file()
    assert isinstance(allowed_groups(), list)
    assert load_expertise().strip()


def test_launcher_works_from_another_directory(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / 'scripts' / 'launch.py'), 'recovery', '--help'],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert '--database' in result.stdout
    assert not list(tmp_path.iterdir())
