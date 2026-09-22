"""Owner-scoped watchlists and alerts; ownerless legacy rows stay historical."""


def ensure_schema(con):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS user_watchlist(
      user_id INTEGER NOT NULL, symbol TEXT NOT NULL, market TEXT NOT NULL,
      added_at TEXT, folder TEXT DEFAULT '', tags TEXT DEFAULT '',
      PRIMARY KEY(user_id,symbol,market));
    CREATE TABLE IF NOT EXISTS user_price_alerts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
      symbol TEXT, market TEXT, kind TEXT, value REAL, created_at TEXT,
      triggered_at TEXT, triggered_price REAL, active INTEGER DEFAULT 1,
      params TEXT DEFAULT '');
    CREATE INDEX IF NOT EXISTS user_price_alerts_owner ON user_price_alerts(user_id,id);
    ''')
