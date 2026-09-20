import json, urllib.request

base = "http://127.0.0.1:8000/api/v1"
pid = "bf69c1e426504874962676acd207ae3f"
d = json.load(urllib.request.urlopen(f"{base}/projects/{pid}/control/summary"))
print("scheduler:", d["scheduler_running"], "| active_runs:", len(d["active_runs"]))
print("project:", d["project"]["name"])
print("--- tasks ---")
for t in d["sprint_tasks"]:
    print(f"  {t['status']:<11} {t['title'][:55]}  rework={t.get('rework_count',0)}")
print("--- recent events ---")
for e in d["events"][:12]:
    p = e.get("payload", {})
    note = p.get("activity") or p.get("note") or p.get("summary") or p.get("outcome") or ""
    print(f"  {e['event_type']:<24} {str(note)[:110]}")
