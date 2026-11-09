from app.dashboard_core_pg import open_readonly, get_dashboard_summary

conn = open_readonly()

try:
    summary = get_dashboard_summary(conn)
    print(summary)
finally:
    conn.close()