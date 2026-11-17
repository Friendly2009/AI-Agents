"""Persistence schema; deliberately independent of transports."""

def migrate(db):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbound_events'").fetchone():
            return
        db.executescript('''
        CREATE TABLE IF NOT EXISTS contact_permission (
            uid INTEGER PRIMARY KEY, source TEXT NOT NULL, granted TEXT NOT NULL,
            status TEXT NOT NULL, declined_at TEXT);
        CREATE TABLE IF NOT EXISTS outbound_operations (
            id TEXT PRIMARY KEY, uid INTEGER NOT NULL, kind TEXT NOT NULL,
            source TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
            created TEXT NOT NULL, attempted TEXT, error TEXT, message_id INTEGER,
            batch TEXT, simulated INTEGER NOT NULL DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS one_initial_attempt ON outbound_operations(uid)
            WHERE kind='initial' AND attempted IS NOT NULL AND simulated=0;
        CREATE TABLE IF NOT EXISTS outbound_events (
            seq INTEGER PRIMARY KEY, utc TEXT NOT NULL, event TEXT NOT NULL,
            operation TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
            error TEXT NOT NULL, source TEXT NOT NULL);
        ''')
