"""Owner dashboard: action-unit feed + correction box.

render_dashboard is stdlib-only so the P0-5 acceptance test can render
without running a web server; tools/server.py serves the same HTML.
"""

import html
import json


def _reports(store, store_id: str) -> list[dict]:
    reports = []
    for r in store.recent_by_kind(store_id, "owner_report", k=200):
        try:
            reports.append(json.loads(r["meta"]) if r["meta"] else {})
        except (json.JSONDecodeError, TypeError):
            pass
    return reports


def render_dashboard(store, store_id: str) -> str:
    reports = _reports(store, store_id)
    cards = []
    for r in reports:
        items = "".join(
            f"<li><b>{html.escape(i['kind'])}</b>: {html.escape(i['detail'])}</li>"
            for i in r.get("action_items", [])
        )
        cards.append(
            f"<div class='card cat-{html.escape(r.get('category', 'inquiry'))}'>"
            f"<div class='cat'>{html.escape(r.get('category', ''))}</div>"
            f"<div class='sum'>{html.escape(r.get('summary', ''))}</div>"
            f"<div class='meta'>{html.escape(r.get('caller_id', ''))} · "
            f"{html.escape(r.get('finished_at', ''))}</div>"
            f"<ul>{items}</ul></div>"
        )
    body = "".join(cards) or "<p class='empty'>No calls yet.</p>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Missed-call agent — owner feed</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 44rem; background: #0f1115; color: #e8eaed; }}
h1 {{ font-size: 1.3rem; }}
.card {{ background: #1b1f27; border: 1px solid #2a2f3a; border-left: 4px solid #4c8dff; border-radius: 10px; padding: 1rem; margin: .8rem 0; }}
.card.cat-booking_confirmed {{ border-left-color: #2fbf71; }}
.card.cat-callback_needed {{ border-left-color: #f2a93b; }}
.card.cat-spam {{ border-left-color: #888; opacity: .7; }}
.cat {{ text-transform: uppercase; font-size: .7rem; letter-spacing: .08em; color: #9aa4b2; }}
.sum {{ font-size: 1.05rem; margin: .25rem 0; }}
.meta {{ font-size: .75rem; color: #7d8794; }}
ul {{ margin: .5rem 0 0; padding-left: 1.1rem; }}
.correct {{ margin-top: 2rem; padding: 1rem; background: #171b22; border-radius: 10px; }}
input, textarea {{ width: 100%; padding: .5rem; margin: .3rem 0; background: #0f1115; color: #e8eaed; border: 1px solid #2a2f3a; border-radius: 6px; }}
button {{ padding: .5rem 1rem; background: #4c8dff; border: 0; border-radius: 6px; color: #fff; cursor: pointer; }}
</style></head><body>
<h1>Missed-call agent — owner feed</h1>
{body}
<form class="correct" method="post" action="/dashboard/correct" onsubmit="fetch('/dashboard/correct',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{rule_text:this.rule_text.value}})}});return false">
  <b>Owner correction → standing rule</b>
  <textarea name="rule_text" rows="2" placeholder="e.g. auto-book parties of 4 or fewer without confirming"></textarea>
  <button type="submit">Save rule</button>
</form>
</body></html>"""
