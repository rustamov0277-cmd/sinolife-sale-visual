"""
Bitrix24 → {ходим_рақами: РОП номи}
Ходим рақами → email ({рақам}@sinolifemanager.uz) → бўлим → "(ROP)" бўлимигача юқорига.

Кэш: /root/sheets_dashboard/rop_map.json (6 соатда бир янгиланади)
Синаш: python3 rop_map.py
"""

import os, json, time, logging
import urllib.request, urllib.error
from datetime import datetime

log = logging.getLogger(__name__)

WEBHOOK    = os.environ.get("BITRIX_WEBHOOK", "").rstrip("/") + "/"
CACHE_FILE = os.environ.get("ROP_CACHE", "/root/sheets_dashboard/rop_map.json")
CACHE_TTL  = int(os.environ.get("ROP_CACHE_TTL", "21600"))   # 6 соат
DOMAIN     = os.environ.get("EMPLOYEE_EMAIL_DOMAIN", "sinolifemanager.uz")


def _bx(method, params=None):
    url = WEBHOOK + method + ".json"
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _paged(method, params):
    out, start = [], 0
    while True:
        p = dict(params or {})
        p["start"] = start
        resp = _bx(method, p)
        if "error" in resp:
            raise RuntimeError(resp.get("error_description", resp["error"]))
        out += resp.get("result", []) or []
        nxt = resp.get("next")
        if not nxt:
            break
        start = nxt
    return out


def _find_rop(dept_id, by_id):
    """Бўлимдан юқорига чиқиб, номида '(ROP)' борини топади."""
    cur, seen = str(dept_id), set()
    while cur and cur not in seen:
        seen.add(cur)
        d = by_id.get(cur)
        if not d:
            return None
        name = d.get("NAME") or ""
        if "(ROP)" in name or "(РОП)" in name:
            return name.replace("(ROP)", "").replace("(РОП)", "").strip() or name
        cur = str(d.get("PARENT") or "")
    return None


def fetch():
    """{рақам: РОП номи}"""
    if not os.environ.get("BITRIX_WEBHOOK"):
        raise RuntimeError("BITRIX_WEBHOOK ўрнатилмаган")
    depts = _bx("department.get", {}).get("result", []) or []
    by_id = {str(d["ID"]): d for d in depts}
    users = _paged("user.get", {})
    out = {}
    for u in users:
        email = (u.get("EMAIL") or "").strip().lower()
        num = email.split("@")[0]
        if not num.isdigit():
            continue
        for dep in (u.get("UF_DEPARTMENT") or []):
            rop = _find_rop(dep, by_id)
            if rop:
                out[num] = rop
                break
    log.info("Bitrix: %d бўлим, %d ходим, %d рақам РОПга боғланди",
             len(depts), len(users), len(out))
    return out
_FAILED = False


def load(force=False):
    """Кэшдан ўқийди, эскирган бўлса Bitrix'дан янгилайди.
    Bitrix ишламаса — шу ишга тушишда бошқа урингмайди (вақт тежалади)."""
    global _FAILED
    cache, ts = {}, 0
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                j = json.load(f)
            cache, ts = j.get("map", {}), j.get("ts", 0)
        except Exception as e:
            log.error("rop кэш ўқилмади: %s", e)

    if not force and cache and (time.time() - ts) < CACHE_TTL:
        return cache
    if _FAILED and not force:
        return cache

    try:
        fresh = fetch()
        if fresh:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "map": fresh}, f, ensure_ascii=False)
            return fresh
    except Exception as e:
        _FAILED = True
        log.error("⚠️ Bitrix'дан РОП олинмади (%s) — эски кэш ишлатилади", str(e)[:150])
    return cache

def num_of(name):
    """'Азиза Вафокулова 112' → '112'"""
    parts = str(name or "").strip().split()
    return parts[-1] if parts and parts[-1].isdigit() else None


def rop_of(name, rmap):
    n = num_of(name)
    return rmap.get(n) if n else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    m = load(force=True)
    print("Жами:", len(m))
    by_rop = {}
    for num, rop in m.items():
        by_rop.setdefault(rop, []).append(num)
    for rop in sorted(by_rop):
        nums = sorted(by_rop[rop], key=lambda x: int(x))
        print("%-28s %3d та: %s" % (rop, len(nums), ", ".join(nums[:25])))
