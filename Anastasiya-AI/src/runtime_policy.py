"""Pure environment policy shared by services and delivery."""
import os

def safe_mode():
    return os.getenv('SAFE_TEST_MODE','true').strip().lower() != 'false'
