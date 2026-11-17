"""Launch application commands from any working directory."""
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = {'main', 'admin_panel', 'doctor', 'reset_outreach', 'evaluate_dialogues',
            'release_gate', 'delivery_report', 'recovery', 'simulate_delivery', 'render_panel_demo'}

if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        raise SystemExit('Usage: python scripts/launch.py COMMAND [arguments]\nCommands: ' + ', '.join(sorted(COMMANDS)))
    command = sys.argv.pop(1)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'src'))
    runpy.run_module(command, run_name='__main__')
