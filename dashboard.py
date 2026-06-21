"""
Sheets -> дашборд (GitHub Pages).
Читает листы: "Dashboard" (все продавцы), "ROP dashboard" (команды),
и любой другой лист -> отдельная страница продавца (новые подхватываются сами).
Колонки: Имя | ЛИД | План | Факт1 | Факт2 | Транзакция | Конверсия | Выполнение плана.
Генерит HTML и пушит в GitHub Pages. Запуск по cron каждые 10 минут.
Ключи/пути - в переменных окружения (см. инструкцию внизу).
"""

import os, json, base64, ssl, urllib.request, urllib.error, logging
from datetime import datetime, timezone, timedelta

import gspread
from google.oauth2.service_account import Credentials

SHEET_ID    = os.environ.get("SHEET_ID", "1pufQB6lW_KrTgh_fEjhZSfNLUurpP8Z5T3maOX8P3tk")
SA_JSON     = os.environ.get("SA_JSON_PATH", "/root/sheets_dashboard/service_account.json")
GITHUB_TOKEN = os.environ.get("DASH_GITHUB_TOKEN", "")
GITHUB_USER = os.environ.get("DASH_GITHUB_USER", "rustamov0277-cmd")
GITHUB_REPO = os.environ.get("DASH_GITHUB_REPO", "sales-visual")
GITHUB_FILE = "index.html"

DASH_SHEET = "Dashboard"
ROP_SHEET  = "ROP dashboard"
TZ = timezone(timedelta(hours=5))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

def open_book():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(SA_JSON, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)

def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace("\xa0", "").replace(" ", "").replace("%", "").replace(",", ".")
    if not s or "DIV" in s or "REF" in s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None

def parse_rows_values(values):
    header_idx = None
    for i, r in enumerate(values[:5]):
        joined = " ".join(c.upper() for c in r)
        if "ЛИД" in joined:
            header_idx = i
            break
    if header_idx is None:
        header_idx = 0
    rows = []
    for r in values[header_idx + 1:]:
        if not r or not r[0].strip():
            continue
        name = r[0].strip()
        def col(i): return r[i] if i < len(r) else ""
        leads = _num(col(1)); plan = _num(col(2))
        fact1 = _num(col(3)); fact2 = _num(col(4))
        trans = _num(col(5)); conv = _num(col(6)); plandone = _num(col(7))
        if all(x is None for x in [leads, plan, fact1, fact2, trans]):
            continue
        rows.append({"name": name, "leads": leads, "plan": plan,
                     "fact1": fact1, "fact2": fact2, "trans": trans,
                     "conv": conv, "plandone": plandone})
    return rows

def parse_rows(ws):
    return parse_rows_values(ws.get_all_values())

def parse_person_values(values):
    """values (list of rows) -> {total, days}."""
    header_idx = None
    for i, r in enumerate(values[:6]):
        joined = " ".join(c.upper() for c in r)
        if "ЛИД" in joined:
            header_idx = i
            break
    if header_idx is None:
        header_idx = 0
    days = []
    total = None
    for r in values[header_idx + 1:]:
        if not r or not r[0].strip():
            continue
        first = r[0].strip()
        def col(i): return r[i] if i < len(r) else ""
        rec = {"date": first,
               "leads": _num(col(1)), "plan": _num(col(2)),
               "fact1": _num(col(3)), "fact2": _num(col(4)),
               "trans": _num(col(5)), "conv": _num(col(6)),
               "plandone": _num(col(7)), "fot": _num(col(8))}
        if first.lower() in ("общий", "итого", "jami", "umumiy", "всего"):
            total = rec
            continue
        if any(rec[k] is not None for k in ("leads", "fact1", "fact2", "trans")):
            days.append(rec)
    if total is None and days:
        def s(k): return sum(d[k] or 0 for d in days)
        leads = s("leads"); plan = s("plan"); fot = s("fot")
        fact1 = s("fact1"); fact2 = s("fact2"); trans = s("trans")
        total = {"date": "Общий", "leads": leads, "plan": plan,
                 "fact1": fact1, "fact2": fact2, "trans": trans,
                 "conv": round(trans/leads*100, 1) if leads else None,
                 "plandone": round(fact2/plan*100, 1) if plan else None,
                 "fot": fot}
    return {"total": total or {}, "days": days}

def safe_ws(book, title):
    try:
        return book.worksheet(title)
    except Exception:
        return None

def collect():
    book = open_book()
    # один запрос метаданных — берём названия листов
    worksheets = book.worksheets()
    titles = [ws.title for ws in worksheets]
    data = {"period": "", "sellers": [], "rops": [], "people": {}}

    # ВСЕ листы одним батч-запросом значений
    ranges = ["'" + t.replace("'", "''") + "'!A1:I60" for t in titles]
    sheets_values = {}
    try:
        batch = book.values_batch_get(ranges)
        for t, vr in zip(titles, batch.get("valueRanges", [])):
            sheets_values[t] = vr.get("values", [])
    except Exception as e:
        log.error("batch_get error: %s", e)
        raise

    # Dashboard
    if DASH_SHEET in sheets_values:
        data["sellers"] = parse_rows_values(sheets_values[DASH_SHEET])
    # ROP dashboard
    if ROP_SHEET in sheets_values:
        rv = sheets_values[ROP_SHEET]
        data["rops"] = parse_rows_values(rv)
        if rv and rv[0]:
            joined = " ".join(rv[0])
            if "202" in joined:
                data["period"] = joined.strip()
    # каждый продавец
    for t in titles:
        if t in (DASH_SHEET, ROP_SHEET):
            continue
        person = parse_person_values(sheets_values.get(t, []))
        if person["total"] or person["days"]:
            data["people"][t] = person
    return data

def generate_html(data):
    updated = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    payload = json.dumps(data, ensure_ascii=False)

    css = (
        "@import url('https://fonts.googleapis.com/css2?family=Unbounded:wght@400;700;900&family=Inter:wght@300;400;500;600&display=swap');"
        ":root{--bg:#0a0e14;--card:#141b24;--line:#26323f;--txt:#eef3f7;--mut:#7e90a2;--accent:#22c55e;--accent2:#06b6d4;}"
        "*{box-sizing:border-box;margin:0;padding:0}"
        "body{background:var(--bg);color:var(--txt);font-family:Inter,sans-serif;min-height:100vh}"
        "header{padding:1.1rem 1.6rem;border-bottom:1px solid var(--line);background:linear-gradient(135deg,#0a0e14,#0d1a22);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem}"
        "header h1{font-family:Unbounded;font-size:1.05rem;font-weight:900;background:linear-gradient(135deg,#22c55e,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent}"
        "header .upd{font-size:.72rem;color:var(--mut)}"
        ".nav{display:flex;gap:.3rem;padding:.9rem 1.6rem 0;border-bottom:1px solid var(--line);overflow-x:auto;scrollbar-width:none}"
        ".nav::-webkit-scrollbar{display:none}"
        ".tab{padding:.45rem .9rem;border-radius:8px 8px 0 0;border:1px solid transparent;border-bottom:none;cursor:pointer;font-size:.74rem;font-weight:500;white-space:nowrap;color:var(--mut);background:transparent}"
        ".tab:hover{color:var(--txt);background:var(--card)}"
        ".tab.active{color:var(--txt);background:var(--card);border-color:var(--line)}"
        ".content{padding:1.4rem 1.6rem}.panel{display:none}.panel.active{display:block}"
        ".period{color:var(--mut);font-size:.78rem;padding:.8rem 1.6rem 0}"
        "table{width:100%;border-collapse:collapse;border-radius:10px;overflow:hidden;border:1px solid var(--line);margin-bottom:1.2rem}"
        "th{padding:.6rem .8rem;text-align:right;font-size:.62rem;color:#9fb0c0;text-transform:uppercase;letter-spacing:.05em;background:#0d141c;border-bottom:2px solid var(--line)}"
        "th:nth-child(2){text-align:left}th:first-child{text-align:center}"
        "td{padding:.7rem .8rem;text-align:right;font-size:.82rem;border-bottom:1px solid #1c2530}"
        "td:nth-child(2){text-align:left;font-weight:600}td:first-child{text-align:center}"
        "tr:last-child td{border-bottom:none}"
        "tbody tr:nth-child(odd) td{background:#0f1620}tbody tr:nth-child(even) td{background:#131c27}"
        "tbody tr:hover td{background:#1a2633!important}"
        ".rank{font-family:Unbounded;font-weight:900;font-size:.8rem;color:var(--mut)}"
        ".g1 td:first-child{border-left:3px solid #f59e0b}.g2 td:first-child{border-left:3px solid #94a3b8}.g3 td:first-child{border-left:3px solid #b45309}"
        ".g1 .rank{color:#f59e0b}.g2 .rank{color:#94a3b8}.g3 .rank{color:#b45309}"
        ".money{font-family:Unbounded;font-weight:700;font-size:.8rem}"
        ".bg{background:rgba(34,197,94,.15);color:#22c55e;padding:.15rem .5rem;border-radius:5px;font-size:.72rem;font-weight:700;display:inline-block}"
        ".br{background:rgba(239,68,68,.15);color:#f87171;padding:.15rem .5rem;border-radius:5px;font-size:.72rem;font-weight:700;display:inline-block}"
        ".by{background:rgba(245,158,11,.15);color:#fbbf24;padding:.15rem .5rem;border-radius:5px;font-size:.72rem;font-weight:700;display:inline-block}"
        ".cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:.7rem;margin-bottom:1.2rem}"
        ".c{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1rem}"
        ".c .l{font-size:.62rem;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin-bottom:.35rem}"
        ".c .v{font-family:Unbounded;font-size:1.15rem;font-weight:700;line-height:1.1}"
        ".bar{height:7px;border-radius:4px;background:#1c2530;min-width:70px;flex:1}"
        ".barf{height:100%;border-radius:4px;background:linear-gradient(90deg,#06b6d4,#22c55e)}"
        ".barf.over{background:linear-gradient(90deg,#22c55e,#84cc16)}"
        ".pw{display:flex;align-items:center;gap:.5rem}.pp{font-family:Unbounded;font-size:.78rem;font-weight:700;min-width:42px;text-align:right}"
        ".empty{color:var(--mut);text-align:center;padding:2rem;font-size:.85rem}"
    )

    js = (
        "var D=" + payload + ";"
        "function money(v){if(v==null)return '-';return Math.round(v).toLocaleString('ru-RU')}"
        "function num(v){if(v==null)return '-';return Math.round(v).toLocaleString('ru-RU')}"
        "function pct(v){if(v==null)return '-';return Math.round(v)+'%'}"
        "function medal(i){return i===0?'1':i===1?'2':i===2?'3':(i+1)}"
        "function rc(i){return i===0?'g1':i===1?'g2':i===2?'g3':''}"
        "function convOf(p){if(p.conv!=null)return p.conv;if(p.leads)return p.trans/p.leads*100;return null}"
        "function planOf(p){if(p.plandone!=null)return p.plandone;if(p.plan)return p.fact2/p.plan*100;return null}"
        "function idInRange(name){var m=(name||'').match(/(\\d{2,4})\\s*$/);if(!m)return false;var id=parseInt(m[1]);return id>=107&&id<=147}"
        "function bonusInfo(name,fact2){"
        "var tiers=[[70000000,2000000],[60000000,1500000],[45000000,1000000]];"
        "if(!idInRange(name))return null;"
        "var cur=0,curTier=0;for(var i=0;i<tiers.length;i++){if(fact2>=tiers[i][0]){cur=tiers[i][1];curTier=tiers[i][0];break}}"
        "var next=null,nextBonus=null;var asc=[[45000000,1000000],[60000000,1500000],[70000000,2000000]];"
        "for(var j=0;j<asc.length;j++){if(fact2<asc[j][0]){next=asc[j][0];nextBonus=asc[j][1];break}}"
        "return {current:cur,next:next,nextBonus:nextBonus,remain:next?next-fact2:0,maxed:cur===2000000}}"
        "function rankTable(rows,title){var r=rows.filter(function(p){return p.fact2!=null}).sort(function(a,b){return (b.fact2||0)-(a.fact2||0)});"
        "if(!r.length)return '<div class=\"empty\">Malumot yo`q</div>';"
        "var body=r.map(function(p,i){var pl=planOf(p);var col=pl==null?'var(--mut)':pl>=100?'#22c55e':pl>=70?'#06b6d4':'#f87171';var fc=pl>=100?' over':'';"
        "var cv=convOf(p);var cb=cv==null?'-':cv>=40?('<span class=\"bg\">'+pct(cv)+'</span>'):cv>=25?('<span class=\"by\">'+pct(cv)+'</span>'):('<span class=\"br\">'+pct(cv)+'</span>');"
        "return '<tr class=\"'+rc(i)+'\"><td class=\"rank\">'+medal(i)+'</td><td>'+p.name+'</td>'"
        "+'<td class=\"money\" style=\"color:#22c55e\">'+money(p.fact2)+'</td>'"
        "+'<td style=\"color:#9fb0c0\">'+money(p.fact1)+'</td>'"
        "+'<td>'+num(p.trans)+'</td>'"
        "+'<td>'+num(p.leads)+'</td>'"
        "+'<td>'+cb+'</td>'"
        "+'<td style=\"min-width:130px\"><div class=\"pw\"><div class=\"bar\"><div class=\"barf'+fc+'\" style=\"width:'+Math.min(pl||0,100)+'%\"></div></div><span class=\"pp\" style=\"color:'+col+'\">'+pct(pl)+'</span></div></td></tr>'}).join('');"
        "return '<table><thead><tr><th>#</th><th>'+title+'</th><th>Uspeshka (Fakt2)</th><th>Zakazlar (Fakt1)</th><th>Tranz.</th><th>Lid</th><th>Konv.</th><th>Plan bajarish</th></tr></thead><tbody>'+body+'</tbody></table>'}"
        "function personPage(name,obj){var p=(obj&&obj.total)||{};var days=(obj&&obj.days)||[];var cv=convOf(p),pl=planOf(p);"
        "var cards='<div class=\"cards\">'"
        "+'<div class=\"c\"><div class=\"l\">Lid</div><div class=\"v\">'+num(p.leads)+'</div></div>'"
        "+'<div class=\"c\"><div class=\"l\">Tranzaksiya</div><div class=\"v\">'+num(p.trans)+'</div></div>'"
        "+'<div class=\"c\"><div class=\"l\">Uspeshka (Fakt2)</div><div class=\"v\" style=\"color:#22c55e;font-size:.95rem\">'+money(p.fact2)+'</div></div>'"
        "+'<div class=\"c\"><div class=\"l\">Zakazlar (Fakt1)</div><div class=\"v\" style=\"font-size:.95rem\">'+money(p.fact1)+'</div></div>'"
        "+'<div class=\"c\"><div class=\"l\">Plan</div><div class=\"v\" style=\"font-size:.95rem;color:#9fb0c0\">'+money(p.plan)+'</div></div>'"
        "+'<div class=\"c\"><div class=\"l\">Konversiya</div><div class=\"v\" style=\"color:#06b6d4\">'+pct(cv)+'</div></div>'"
        "+'<div class=\"c\"><div class=\"l\">Plan bajarish</div><div class=\"v\" style=\"color:'+(pl>=100?'#22c55e':'#06b6d4')+'\">'+pct(pl)+'</div></div>'"
        "+'<div class=\"c\" style=\"border-color:#3a2f0a\"><div class=\"l\">FOT (ish haqi)</div><div class=\"v\" style=\"color:#fbbf24;font-size:.95rem\">'+money(p.fot)+'</div></div>'"
        "+'</div>';"
        "var bi=bonusInfo(name,p.fact2||0);var bonusBlock='';"
        "if(bi){var bcol=bi.current>0?'#22c55e':'#7e90a2';"
        "var line1='<div style=\"font-size:.7rem;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin-bottom:.5rem\">Bonus</div>';"
        "var line2='<div style=\"display:flex;align-items:baseline;gap:.5rem;margin-bottom:.6rem\"><span style=\"font-family:Unbounded;font-size:1.4rem;font-weight:800;color:'+bcol+'\">'+money(bi.current)+'</span><span style=\"font-size:.75rem;color:var(--mut)\">hozirgi bonus</span></div>';"
        "var line3='';"
        "if(bi.maxed){line3='<div style=\"color:#22c55e;font-size:.85rem;font-weight:600\">Maksimal bonusga yetdingiz!</div>'}"
        "else if(bi.next){var pctp=Math.min((p.fact2||0)/bi.next*100,100);"
        "line3='<div style=\"font-size:.82rem;color:#eef3f7;margin-bottom:.4rem\">Keyingi bonus <b style=\"color:#fbbf24\">'+money(bi.nextBonus)+'</b> uchun yana <b style=\"color:#06b6d4\">'+money(bi.remain)+' sum</b> kerak</div>'"
        "+'<div style=\"height:9px;border-radius:5px;background:#1c2530\"><div style=\"height:100%;border-radius:5px;width:'+pctp+'%;background:linear-gradient(90deg,#06b6d4,#22c55e)\"></div></div>'"
        "+'<div style=\"font-size:.68rem;color:var(--mut);margin-top:.3rem\">'+money(p.fact2)+' / '+money(bi.next)+'</div>'}"
        "bonusBlock='<div style=\"background:var(--card);border:1px solid #3a2f0a;border-radius:12px;padding:1.1rem;margin-top:1rem\">'+line1+line2+line3+'</div>'}"
        "var chartId='ch_'+name.replace(/[^a-zA-Z0-9]/g,'');"
        "var chartBlock=days.length?('<div style=\"background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1rem;margin-top:1rem\"><div style=\"font-size:.7rem;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin-bottom:.7rem\">Kunlik dinamika: Lid, Fakt1 va Fakt2</div><canvas id=\"'+chartId+'\" height=\"90\"></canvas></div>'):'';"
        "return cards+bonusBlock+chartBlock}"
        "function drawCharts(){Object.keys(D.people||{}).forEach(function(name){var obj=D.people[name];var days=(obj&&obj.days)||[];if(!days.length)return;var cid='ch_'+name.replace(/[^a-zA-Z0-9]/g,'');var cv=document.getElementById(cid);if(!cv||cv.dataset.done)return;cv.dataset.done='1';"
        "var labels=days.map(function(d){return (d.date||'').slice(0,5)});"
        "var leads=days.map(function(d){return d.leads||0});var sales=days.map(function(d){return (d.fact2||0)/1000000});var orders=days.map(function(d){return (d.fact1||0)/1000000});"
        "new Chart(cv,{type:'bar',data:{labels:labels,datasets:["
        "{type:'line',label:'Lid',data:leads,borderColor:'#06b6d4',backgroundColor:'#06b6d4',yAxisID:'y1',tension:.3,pointRadius:2,order:0},"
        "{type:'bar',label:'Zakazlar Fakt1 (mln)',data:orders,backgroundColor:'#f59e0b88',borderColor:'#f59e0b',yAxisID:'y',order:2},"
        "{type:'bar',label:'Uspeshka Fakt2 (mln)',data:sales,backgroundColor:'#22c55e88',borderColor:'#22c55e',yAxisID:'y',order:1}]},"
        "options:{responsive:true,plugins:{legend:{labels:{color:'#9fb0c0',font:{size:10}}}},scales:{"
        "x:{ticks:{color:'#64748b',font:{size:9}},grid:{color:'#1c2530'}},"
        "y:{position:'left',ticks:{color:'#22c55e',font:{size:9}},grid:{color:'#1c2530'}},"
        "y1:{position:'right',ticks:{color:'#06b6d4',font:{size:9}},grid:{display:false}}}}})})}"
        "var nav=document.getElementById('nav'),content=document.getElementById('content');"
        "var tabs=[];"
        "tabs.push(['Sotuvchilar',function(){return rankTable(D.sellers||[],'Sotuvchi')}]);"
        "tabs.push(['Komandalar',function(){return rankTable(D.rops||[],'ROP / Komanda')}]);"
        "tabs.forEach(function(t,i){var b=document.createElement('button');b.className='tab'+(i===0?' active':'');b.textContent=t[0];b.onclick=(function(i){return function(){sw(i)}})(i);nav.appendChild(b);var pn=document.createElement('div');pn.className='panel'+(i===0?' active':'');pn.id='p'+i;pn.innerHTML=t[1]();content.appendChild(pn)});"
        "var names=Object.keys(D.people||{}).sort();"
        "names.forEach(function(name,k){var idx=k+2;var b=document.createElement('button');b.className='tab';b.textContent=name;b.onclick=(function(idx){return function(){sw(idx)}})(idx);nav.appendChild(b);var pn=document.createElement('div');pn.className='panel';pn.id='p'+idx;pn.innerHTML='<h2 style=\"font-family:Unbounded;font-size:1rem;margin-bottom:1rem\">'+name+'</h2>'+personPage(name,D.people[name]);content.appendChild(pn)});"
        "function sw(i){document.querySelectorAll('.tab').forEach(function(t,k){t.classList.toggle('active',k===i)});document.querySelectorAll('.panel').forEach(function(p,k){p.classList.toggle('active',k===i)});if(window.Chart)drawCharts()}"
        "setTimeout(function(){location.reload()},600000);"
        "if(window.Chart)drawCharts();"
    )

    period = data.get("period") or ""
    return ('<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Sotuvchilar dashboard</title>'
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>'
            '<style>' + css + '</style></head><body>'
            '<header><h1>Sotuvchilar dashboardi</h1><span class="upd">Yangilandi: ' + updated + '</span></header>'
            '<div class="period">' + period + '</div>'
            '<div class="nav" id="nav"></div><div class="content" id="content"></div>'
            '<script>' + js + '</script></body></html>')

def push_github(html):
    if not GITHUB_TOKEN:
        log.error("DASH_GITHUB_TOKEN yoq - push qilinmadi")
        return False
    api = "https://api.github.com/repos/" + GITHUB_USER + "/" + GITHUB_REPO + "/contents/" + GITHUB_FILE
    headers = {"Authorization": "token " + GITHUB_TOKEN,
               "Accept": "application/vnd.github.v3+json",
               "User-Agent": "sheets-dashboard"}
    ctx = ssl._create_unverified_context()
    sha = None
    try:
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, context=ctx) as r:
            sha = json.loads(r.read())["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log.error("SHA error: %s", e)
    payload = {"message": "dashboard " + datetime.now(TZ).strftime("%d.%m %H:%M"),
               "content": base64.b64encode(html.encode()).decode()}
    if sha:
        payload["sha"] = sha
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(api, data=data, headers=headers, method="PUT")
        with urllib.request.urlopen(req, context=ctx) as r:
            log.info("GitHub push OK: %s", r.status)
            return True
    except Exception as e:
        log.error("push error: %s", e)
        return False

if __name__ == "__main__":
    import time
    attempts = 0
    while True:
        attempts += 1
        try:
            data = collect()
            log.info("Oqildi: sotuvchi=%d, komanda=%d, shaxsiy varaq=%d",
                     len(data["sellers"]), len(data["rops"]), len(data["people"]))
            html = generate_html(data)
            with open("/root/sheets_dashboard/index.html", "w", encoding="utf-8") as f:
                f.write(html)
            push_github(html)
            break
        except Exception as e:
            msg = str(e)
            if "429" in msg and attempts < 4:
                log.error("429 limit — 65 сония кутиб қайта урунаман (urinish %d)", attempts)
                time.sleep(65)
                continue
            log.error("FATAL: %s", e)
            raise
