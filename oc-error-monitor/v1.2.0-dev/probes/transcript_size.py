"""Measure how big each session's transcript is in the OpenCode database.

The OpenChamber renderer re-renders the whole chat on every streamed delta, so
a fat transcript is what makes the UI busy (and the cursor spin).
"""
import os
import sqlite3
import time

db = os.path.expanduser(r"~\.local\share\opencode\opencode.db")
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

print("== 最近的会话 ==")
for r in con.execute(
    "select id,title,time_created from session_v2 order by time_created desc limit 5"
):
    ts = time.strftime("%m-%d %H:%M", time.localtime((r["time_created"] or 0) / 1000))
    print(f"  {r['id'][:40]}  {ts}  {(r['title'] or '')[:50]}")

print("\n== 记录体积 Top 8 ==")
q = """
select s.id, s.title, s.time_created,
       sum(length(coalesce(m.data,''))) as sz,
       count(*) as msgs
  from session_v2 s join session_message m on m.session_id = s.id
 group by s.id
 order by sz desc
 limit 8
"""
for r in con.execute(q):
    ts = time.strftime("%m-%d %H:%M", time.localtime((r["time_created"] or 0) / 1000))
    print(
        f"  {r['id'][:36]}  {r['sz']/1024:9.1f} KB  {r['msgs']:6d} msgs  {ts}  {(r['title'] or '')[:26]}"
    )

print("\n== 最近更新的那个会话 ==")
r = con.execute(
    """select m.session_id, sum(length(coalesce(m.data,''))) sz, count(*) msgs
         from session_message m group by m.session_id
        order by max(m.rowid) desc limit 1"""
).fetchone()
if r:
    kb = r["sz"] / 1024
    print(f"  {r['session_id']}  {kb:.1f} KB  {r['msgs']} msgs")
    print(f"  若每个 delta 重渲染整条记录，20 delta/s 约等于 {kb*20/1024:.1f} MB/s 写入")
