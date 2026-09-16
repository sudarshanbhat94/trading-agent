"""Read-only session accounting; offsets are normalized by SQLite."""
def session_pnl(con, market, equity, day, epoch, capital, has_positions):
    midnight = day + "T00:00:00+05:30"
    row = con.execute(
        "SELECT equity FROM v2_equity WHERE market=? AND date LIKE 'LIVE_%' "
        "AND julianday(substr(date,6))>=julianday(?) "
        "AND julianday(substr(date,6))<julianday(?) "
        "ORDER BY julianday(substr(date,6)) DESC LIMIT 1",
        (market, epoch, midnight)).fetchone()
    if row:
        return equity - float(row[0])
    # A fresh cash-only epoch has an exact opening value. If overnight
    # exposure existed, cost basis is not a valid substitute for a daily mark.
    overnight = con.execute(
        "SELECT COUNT(*) FROM v2_trades WHERE market=? AND entry_date<? "
        "AND exit_date>=? AND julianday(closed_at)>=julianday(?)",
        (market, day, day, epoch)).fetchone()[0]
    overnight_positions = con.execute("SELECT COUNT(*) FROM v2_positions WHERE market=? AND entry_date<?",
                                      (market, day)).fetchone()[0]
    if overnight_positions or overnight:
        return None
    prior = con.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=? "
        "AND julianday(closed_at)>=julianday(?) AND julianday(closed_at)<julianday(?)",
        (market, epoch, midnight)).fetchone()[0]
    return equity - (capital + float(prior or 0))
