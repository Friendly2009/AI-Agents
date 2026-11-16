"""Application-owned expertise, loaded for each consultation."""
from pathlib import Path

def load_expertise():
    return (Path(__file__).parent / 'sales_expertise.md').read_text(encoding='utf-8')
