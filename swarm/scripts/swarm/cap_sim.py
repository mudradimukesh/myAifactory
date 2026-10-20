import collections
import glob
import json
from itertools import pairwise

files = glob.glob("/Users/mukeshkumarmudradi/.claude/projects/*floorplan*/*.jsonl") + glob.glob(
    "/Users/mukeshkumarmudradi/.claude/projects/*dotfiles-home--agents-skills/*.jsonl"
)
sessions = []
rows_total = 0
ids = set()
models = collections.Counter()
dates = set()
ttl = collections.Counter()
for f in files:
    us = []
    seen = set()
    with open(f) as transcript:
        for line in transcript:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") != "assistant":
                continue
            m = r.get("message", {})
            u = m.get("usage")
            if not u:
                continue
            rows_total += 1
            mid = m.get("id")
            if mid in seen:
                continue  # streaming rows repeat one API message
            seen.add(mid)
            ids.add(mid)
            models[m.get("model", "?")] += 1
            dates.add((r.get("timestamp") or "")[:10])
            c = u.get("cache_creation") or {}
            ttl["1h"] += c.get("ephemeral_1h_input_tokens", 0)
            ttl["5m"] += c.get("ephemeral_5m_input_tokens", 0)
            us.append(
                (
                    u.get("input_tokens", 0),
                    u.get("cache_creation_input_tokens", 0),
                    u.get("cache_read_input_tokens", 0),
                    m.get("model", "?"),
                )
            )
    if len(us) >= 40:
        sessions.append(us)
print(
    "rows",
    rows_total,
    "distinct message ids",
    len(ids),
    "sessions>=40 dedup calls",
    len(sessions),
    "calls",
    sum(len(s) for s in sessions),
)
print("models", dict(models))
print("dates", sorted(dates))
print("ttl creation tokens", dict(ttl))
READ = {"opus": 0.05, "sonnet": 0.1, "fable": 0.025}


def rp(model):
    for k, v in READ.items():
        if k in model:
            return v
    return 0.1


W = 2.0


def ctx(u):
    return u[0] + u[1] + u[2]


decs = []
dectot = 0
for us in sessions:
    for a, b in pairwise(us):
        d = ctx(b) - ctx(a)
        if d < 0:
            decs.append(d)
            dectot += -d
print("context decreases", len(decs), "total", dectot)


def simulate(us, cap, B=38000, keep_dec=True, reuse=0):
    cost = 0
    c = B
    prev = ctx(us[0])
    r = rp(us[0][3])
    cost += (B - reuse) * W + reuse * r
    restarts = 0
    for u in us[1:]:
        d = ctx(u) - prev
        prev = ctx(u)
        r = rp(u[3])
        if not keep_dec:
            d = max(d, 0)
        if c + d > cap:
            restarts += 1
            c = B
            cost += (B - reuse) * W + reuse * r
        if d > 0:
            cost += c * r + d * W
            c += d
        else:
            cost += c * r
            c = max(B, c + d)
    return cost, restarts


def run(label, **kw):
    out = {}
    for cap in (60000, 80000, 100000, 120000, 10**9):
        out[cap] = sum(simulate(us, cap, **kw)[0] for us in sessions)
    rs = {cap: sum(simulate(us, cap, **kw)[1] for us in sessions) for cap in out}
    print(
        f"{label:<48}"
        + "  ".join(
            f"{cap // 1000 if cap < 10**9 else 'none':>5}: {out[cap] / out[10**9]:.3f} ({rs[cap]}r)"
            for cap in out
        )
    )


run("dedup, decreases kept, model prices (0.05/0.1/0.025)")
run(
    "dedup, decreases kept, flat 0.1 read",
) if False else None
run("dedup, decreases zeroed, model prices", keep_dec=False)
run("dedup, decreases kept, 26.6K prefix reuse on restart", reuse=26600)
run("dedup, kept, reuse, baseline 50K", reuse=26600, B=50000)
