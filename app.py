import json
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

URL = "https://cdn.tsetmc.com/api/Instrument/GetInstrumentOptionMarketWatch/1"

HTML = r"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>اسکنر اختیار معامله</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#101827;color:#edf2f7;font-family:Tahoma,Arial,sans-serif}
header{padding:22px 16px;background:#17243a;border-bottom:1px solid #34445e}
h1{font-size:21px;margin:0 0 8px}
small{color:#aab9cc}
main{padding:15px;max-width:850px;margin:auto}
button{width:100%;padding:16px;border:0;border-radius:12px;background:#16865b;color:white;font-size:17px;font-weight:bold}
button:disabled{opacity:.5}
.panel{background:#1a2940;border:1px solid #34445e;border-radius:13px;padding:14px;margin:13px 0}
.status{font-size:16px;font-weight:bold;margin-bottom:8px}
.muted{color:#aab9cc;font-size:13px;line-height:1.9}
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.stat{background:#22334d;padding:12px;border-radius:10px}
.stat b{display:block;font-size:22px;margin-top:7px}
.card{background:#1a2940;border:1px solid #34445e;border-radius:12px;padding:14px;margin:12px 0}
.card h3{margin:0 0 12px;font-size:17px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.cell{background:#22334d;padding:9px;border-radius:8px;font-size:13px;line-height:1.8}
.good{color:#55d6a0}.bad{color:#ff8585}.warn{color:#ffd27a}
.tag{display:inline-block;padding:5px 9px;border-radius:7px;background:#34445e;font-size:12px}
footer{text-align:center;color:#91a2b8;font-size:12px;padding:20px}
</style>
</head>
<body>
<header>
<h1>اسکنر اختیار معامله ایران</h1>
<small>نسخه آزمایشی · فقط خواندنی · بدون ثبت سفارش</small>
</header>
<main>
<div class="panel">
<div class="status" id="status">آماده بررسی بازار</div>
<div class="muted" id="time">هنوز اطلاعات تازه دریافت نشده است.</div>
</div>
<div class="panel">
<h3>تنظیمات هزینه معامله</h3>
<p class="muted">نرخ‌ها را فقط بر اساس اطلاعات معتبر کارگزاری وارد کن. تا آن زمان هدف سود خالص محاسبه نمی‌شود.</p>
<div class="grid">
<label class="cell">کارمزد خرید (٪)
<input id="buyFee" type="number" min="0" max="100" step="0.01" placeholder="نامشخص" style="width:100%;padding:9px;margin-top:6px">
</label>
<label class="cell">کارمزد فروش (٪)
<input id="sellFee" type="number" min="0" max="100" step="0.01" placeholder="نامشخص" style="width:100%;padding:9px;margin-top:6px">
</label>
<label class="cell">لغزش قیمت خروج (٪)
<input id="slippage" type="number" min="0" max="100" step="0.01" placeholder="نامشخص" style="width:100%;padding:9px;margin-top:6px">
</label>
</div>
</div>
<button id="scan" onclick="scan()">دریافت و بررسی بازار</button>
<div class="panel">
<div class="stats">
<div class="stat">تعداد رکوردهای دریافتی<b id="total">—</b></div>
<div class="stat">قراردادهای واجد شرایط<b id="count">—</b></div>
</div>
<p class="muted" id="decision">برای شروع، دکمه دریافت اطلاعات را بزن.</p>
</div>
<div class="panel">
<h3>قراردادهای CALL</h3>
<div id="calls" class="muted">هنوز بررسی نشده است.</div>
</div>
<div class="panel">
<h3>قراردادهای PUT</h3>
<div id="puts" class="muted">هنوز بررسی نشده است.</div>
</div>

<div class="panel">
<h3>تحلیل پیشرفته اختیار معامله</h3>
<p class="muted">
نتایج این بخش از فایل تحلیل ذخیره‌شده خوانده می‌شوند؛
لزوماً با اطلاعات اسکن فعلی یکسان نیستند.
</p>
<button id="loadAnalysis" onclick="loadAnalysis()">
نمایش تحلیل ذخیره‌شده
</button>
<p class="muted" id="analysisStatus">
هنوز تحلیل بارگذاری نشده است.
</p>
<div id="analysisResults"></div>
</div>
<div class="panel">
<h3>قواعد ایمنی</h3>
<div class="muted">
• حداقل ۲۱ روز تا سررسید<br>
• حداکثر دوره نگهداری برنامه‌ریزی‌شده ۷ روز<br>
• حداکثر زیان برنامه‌ریزی‌شده ۳٪ مبلغ خرید، فقط به‌عنوان هشدار خروج دستی<br>
• سفارش فروش و خرید باید معتبر و دارای حجم باشد<br>
• داده قدیمی یا نامطمئن به معنی عدم معامله است<br>
• این نسخه هنوز جهت روند سهم پایه و Greeks را تأیید نمی‌کند.
</div>
</div>
</main>
<footer>اطلاعات صرفاً جهت بررسی است؛ تصمیم و سفارش فقط با تأیید دستی شما.</footer>
<script>
function fmt(x){return Number(x).toLocaleString('fa-IR',{maximumFractionDigits:2})}
function targetInfo(x){
const buyFee = document.getElementById('buyFee').value;
const sellFee = document.getElementById('sellFee').value;
const slip = document.getElementById('slippage').value;

if(buyFee==='' || sellFee==='' || slip===''){
return 'برای محاسبه قیمت هدف سود ۱۰٪ و حد ضرر ۳٪، نرخ کارمزد خرید، کارمزد فروش و لغزش را وارد کنید.';
}
const bf=Number(buyFee)/100, sf=Number(sellFee)/100, sl=Number(slip)/100;
if(![bf,sf,sl].every(v=>Number.isFinite(v)&&v>=0&&v<1)){
return 'نرخ‌ها باید معتبر و کمتر از ۱۰۰٪ باشند.';
}
const cost=x.ask*x.contract_size*(1+bf);
const denominator=x.contract_size*(1-sf)*(1-sl);
if(cost<=0 || denominator<=0) return 'اطلاعات برای محاسبه معتبر نیست.';

const targetBid=cost*1.10/denominator;
const stopBid=cost*0.97/denominator;

return 'قیمت فروش هدف با سود خالص ۱۰٪: '+fmt(targetBid/10)+' تومان'
+' | قیمت فروش در حد زیان ۳٪: '+fmt(stopBid/10)+' تومان'
+' (هر دو تخمینی و بر اساس Bid، با فرض کارمزد درصدی و لغزش واردشده؛ حد ضرر تضمین زیان حداکثر ۳٪ نیست.)';
}
function element(tag,className,text){
const node=document.createElement(tag);
if(className)node.className=className;
if(text!==undefined)node.textContent=text;
return node;
}
function card(x){
const node=element('div','card');
node.dataset.targetKey=x.type+':'+x.symbol;
const title=element('h3',null,x.symbol+' ');
title.append(element('span','tag',x.type));
const grid=element('div','grid');
for(const [label,value] of [
['روز تا سررسید',fmt(x.days)],
['قیمت سهم پایه',fmt(x.spot/10)+' تومان'],
['قیمت فروش ما (Bid)',fmt(x.bid/10)+' تومان'],
['قیمت خرید ما (Ask)',fmt(x.ask/10)+' تومان'],
['حجم سفارش خرید',fmt(x.bid_qty)],
['حجم سفارش فروش',fmt(x.ask_qty)],
['فاصله خرید و فروش',fmt(x.spread)+'٪'],
['قیمت اعمال',fmt(x.strike/10)+' تومان'],
['ضریب قرارداد',fmt(x.contract_size)],
['هزینه خرید یک قرارداد (بدون کارمزد)',fmt(x.ask*x.contract_size/10)+' تومان']
]){
const cell=element('div','cell',label);
cell.append(element('br'),element('b',null,value));
grid.append(cell);
}
node.append(title,grid,element('p','muted target-info',targetInfo(x)),
element('p','muted','ارزیابی: '+x.note));
return node;
}
function renderCards(id,items){
const container=document.getElementById(id);
if(items.length)container.replaceChildren(...items.map(card));
else container.textContent='قرارداد واجد شرایطی برای نمایش وجود ندارد.';
}
function refreshTargets(){
document.querySelectorAll('.card[data-target-key]').forEach(card=>{
const key=card.getAttribute('data-target-key');
const item=window.lastScanItems?.get(key);
const target=card.querySelector('.target-info');
if(item && target) target.textContent=targetInfo(item);
});
}
['buyFee','sellFee','slippage'].forEach(id=>{
document.getElementById(id).addEventListener('input',refreshTargets);
});
async function scan(){
let b=document.getElementById('scan');
b.disabled=true;b.textContent='در حال دریافت اطلاعات...';
document.getElementById('status').textContent='در حال اتصال به TSETMC';
try{
let r=await fetch('/api/scan');
let d=await r.json();
if(!r.ok)throw Error(d.error||'خطا در دریافت اطلاعات');
document.getElementById('total').textContent=fmt(d.total);
document.getElementById('count').textContent=fmt(d.count);
document.getElementById('time').textContent='زمان دریافت snapshot: '+d.fetched_at+' | سن دریافت: '+fmt(d.snapshot_age_seconds)+' ثانیه';
document.getElementById('status').textContent =
d.snapshot_status==='stale' ? 'داده قدیمی؛ بررسی متوقف شد' :
d.snapshot_status==='warning' ? 'هشدار: سن دریافت داده بیش از ۵ دقیقه' :
'دریافت snapshot انجام شد';

document.getElementById('decision').textContent =
d.snapshot_warning + ' تعداد قراردادهای عبورکرده از فیلتر اولیه: ' + fmt(d.count) +
(d.count ? '؛ این به معنی سیگنال خرید نیست.' : '');

window.lastScanItems=new Map([...d.calls,...d.puts].map(x=>[x.type+':'+x.symbol,x]));
renderCards('calls',d.calls);
renderCards('puts',d.puts);
}catch(e){
document.getElementById('status').replaceChildren(element('span','bad','دریافت ناموفق'));
document.getElementById('decision').textContent=e.message;
}finally{b.disabled=false;b.textContent='دریافت و بررسی دوباره';}
}

function analysisValue(x){
const n=Number(x);
return x===null || x===undefined || x==='' ||
       !Number.isFinite(n) ? 'نامشخص' : fmt(n);
}
function analysisCard(x){
const symbol=String(x.symbol||'نامشخص');
const type=String(x.option_type||'نامشخص');
const iv=analysisValue(x.iv_mid);
const delta=analysisValue(x.delta);
const reason=String(x.iv_failure_reason||'ثبت نشده');

const card=document.createElement('div');
card.className='card';

const title=document.createElement('h3');
title.textContent=symbol+' | '+type;
card.appendChild(title);

const details=document.createElement('div');
details.className='muted';
details.textContent=
'IV میانی: '+iv+'٪ | Delta: '+delta+
' | علت خطای IV: '+reason;
card.appendChild(details);

return card;
}
async function loadAnalysis(){
const button=document.getElementById('loadAnalysis');
const status=document.getElementById('analysisStatus');
const results=document.getElementById('analysisResults');

button.disabled=true;
status.textContent='در حال خواندن تحلیل ذخیره‌شده...';
results.replaceChildren();

try{
const response=await fetch('/api/analysis');
const data=await response.json();
if(!response.ok) throw Error(data.error||'خطا در دریافت تحلیل');

status.textContent=
'تعداد: '+fmt(data.count)+
' | Snapshot: '+(data.snapshot_received_at||'نامشخص')+
' | وضعیت: '+data.analysis_status+
' | مدل: '+data.model_status+
' | سیگنال قابل معامله: خیر';

const warning=document.createElement('p');
warning.className='warn';
warning.textContent=
'این نتایج تاریخی/آزمایشی هستند؛ تازگی مظنه‌ها و اعتبار فرض‌های مالی تأیید نشده است.';
results.appendChild(warning);

const contracts=Array.isArray(data.contracts)?data.contracts:[];
contracts.forEach(x=>results.appendChild(analysisCard(x)));

}catch(e){
status.textContent='خطا: '+e.message;
}finally{
button.disabled=false;
}
}
</script>
</body>
</html>"""

def scan_market():
    req = urllib.request.Request(
        URL,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        if response.status != 200:
            raise ValueError("پاسخ معتبر از TSETMC دریافت نشد.")
        raw_data = response.read()
        snapshot_received_at = datetime.now().astimezone()
        data = json.loads(raw_data.decode("utf-8"))

    rows = data.get("instrumentOptMarketWatch")
    if not isinstance(rows, list) or not rows:
        raise ValueError("فهرست قراردادها خالی است.")

    result = {"calls": [], "puts": []}
    for r in rows:
        days = int(r.get("remainedDay") or 0)
        if days < 21:
            continue

        for side, label in (("C", "CALL"), ("P", "PUT")):
            try:
                bid = float(r.get("pMeDem_" + side) or 0)
                ask = float(r.get("pMeOf_" + side) or 0)
                bq = int(r.get("qTitMeDem_" + side) or 0)
                aq = int(r.get("qTitMeOf_" + side) or 0)
                spot = float(r.get("pDrCotVal_UA") or r.get("pClosing_UA") or 0)
                strike = float(r.get("strikePrice") or 0)
                contract_size = int(r.get("contractSize") or 0)
                if bid <= 0 or ask <= 0 or ask < bid or bq <= 0 or aq <= 0:
                    continue
                if contract_size <= 0:
                    continue
                mid = (bid + ask) / 2
                spread = (ask - bid) / mid * 100
                if spread > 5:
                    continue
                if spot <= 0 or strike <= 0:
                    continue
                intrinsic = max(spot-strike, 0) if side == "C" else max(strike-spot, 0)
                if ask - intrinsic < 0:
                    continue
                item = {
                    "symbol": r.get("lVal18AFC_" + side, "نامشخص"),
                    "type": label, "days": days, "bid": bid, "ask": ask,
                    "bid_qty": bq, "ask_qty": aq, "spot": spot,
                    "strike": strike, "contract_size": contract_size,
                    "spread": round(spread, 2),
                    "note": "فقط عبور از فیلتر اولیه؛ روند، Greeks و تازگی مظنه باید جداگانه تأیید شوند."
                }
                result["calls" if side == "C" else "puts"].append(item)
            except (TypeError, ValueError):
                continue

    result["calls"].sort(key=lambda x: x["spread"])
    result["puts"].sort(key=lambda x: x["spread"])
    now = datetime.now().astimezone()
    age_seconds = max(0, (now - snapshot_received_at).total_seconds())

    result["total"] = len(rows)
    result["snapshot_received_at"] = snapshot_received_at.isoformat(timespec="seconds")
    result["snapshot_age_seconds"] = round(age_seconds)
    result["snapshot_status"] = (
        "fresh" if age_seconds <= 300 else
        "warning" if age_seconds <= 900 else
        "stale"
    )

    if age_seconds > 900:
        result["calls"] = []
        result["puts"] = []
        result["snapshot_warning"] = "دریافت داده بیش از ۱۵ دقیقه قبل انجام شده؛ فعلاً معامله نکن."
    elif age_seconds > 300:
        result["snapshot_warning"] = "سن دریافت snapshot بین ۵ تا ۱۵ دقیقه است؛ تازگی مظنه‌ها تأیید نشده است."
    else:
        result["snapshot_warning"] = "زمان دریافت snapshot تازه است؛ اما زمان به‌روزرسانی واقعی هر مظنه مشخص نیست."

    result["count"] = len(result["calls"]) + len(result["puts"])
    result["fetched_at"] = snapshot_received_at.isoformat(timespec="seconds")
    return result


def load_analysis():
    from pathlib import Path

    path = Path(__file__).resolve().parent / "data" / "greeks_results.json"
    rows = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(rows, list):
        raise ValueError("Analysis data must be a list.")

    snapshots = {
        r.get("snapshot_received_at")
        for r in rows
        if isinstance(r, dict) and r.get("snapshot_received_at")
    }

    if len(snapshots) != 1 or len(rows) == 0:
        status = "unknown"
        snapshot = None
    else:
        snapshot = next(iter(snapshots))
        try:
            dt = datetime.fromisoformat(snapshot)
            if dt.tzinfo is None:
                raise ValueError("Timezone missing")
            age = (datetime.now().astimezone() - dt).total_seconds()
            status = "historical" if age > 900 else "recent_unverified"
        except ValueError:
            status = "unknown"

    return {
        "count": len(rows),
        "snapshot_received_at": snapshot,
        "analysis_status": status,
        "model_status": "experimental_unverified",
        "tradable_signal": False,
        "warning": (
            "Greeks are experimental. Quote freshness and "
            "financial assumptions are not validated."
        ),
        "contracts": rows,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/api/analysis":
            try:
                body = json.dumps(
                    load_analysis(), ensure_ascii=False
                ).encode("utf-8")
                self.send_response(200)
            except Exception as e:
                body = json.dumps(
                    {"error": str(e)}, ensure_ascii=False
                ).encode("utf-8")
                self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        elif self.path == "/api/scan":
            try:
                body = json.dumps(scan_market(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
            except Exception as e:
                body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        else:
            body = b"Not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[APP]", fmt % args)

if __name__ == "__main__":
    print("اسکنر آماده است: http://127.0.0.1:8765")
    print("برای توقف سرور: Ctrl+C")
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
