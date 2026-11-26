"""Exercise the legacy dashboard and pilot pages in headless Chrome."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from http.client import HTTPConnection
import shutil
import subprocess
from pathlib import Path
import threading
from tempfile import TemporaryDirectory

from app import HTML
from shadow_pilot import atomic_json, load_state, private_directory
from shadow_pilot_dashboard import HOST, PilotServer


DAY = "2026-11-22"


def pilot_clock():
    return datetime(2026, 11, 22, 6, tzinfo=timezone.utc)


LEGACY_REPORT = {
    "تاریخ تهران": DAY,
    "صلاحیت اجرای مشاهده": False,
    "زمان گزارش": "2026-11-22T09:30:00+03:30",
    "مشاهدات و غربال پژوهشی": {"آخرین ثبت": None, "پوشش": {}},
}

PILOT_SCENARIOS = {
    "missing": (None, "گزارش این تاریخ هنوز منتشر نشده است.",
                "منتظر اجرای بعدی زمانبند بمانید"),
    "legacy": (LEGACY_REPORT, "نسخهٔ قدیمی گزارش جدول قراردادها را ندارد.",
               "اجازه دهید پایلوت بهروز شده"),
    "invalid": ({}, "گزارش این تاریخ نامعتبر است.",
                "فایل report را دستکاری نکنید"),
}


@contextmanager
def pilot_server(root):
    server = PilotServer(root, 0, clock=pilot_clock)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def fetch_page(server, path="/"):
    client = HTTPConnection(HOST, server.server_port, timeout=5)
    try:
        client.request("GET", path)
        response = client.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        client.close()


def render_chrome(chrome, page, profile):
    return subprocess.run([
        chrome, "--headless", "--disable-background-networking", "--disable-component-update",
        "--no-first-run", "--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND",
        f"--user-data-dir={profile}", "--dump-dom", page.as_uri(),
    ], capture_output=True, encoding="utf-8", timeout=45)


def build_success_report():
    """Generate a valid report through the existing synthetic pilot fixture."""
    from test_shadow_pilot import PilotTests

    fixture = PilotTests()
    fixture.setUp()
    try:
        fixture.enable_paper()
        fixture.sign(1, entries=[("browser", 1)])
        report = fixture.report()
    finally:
        fixture.doCleanups()
    original = report["مشاهدات و غربال پژوهشی"]["قراردادها"]["202"]
    rows = {str(1000 + index): dict(
        original, symbol=f"browser-{index:02}",
        option_type="CALL" if index % 2 == 0 else "PUT",
        candidate_eligible=index % 2 == 0,
        candidate_rejection=None if index % 2 == 0 else "insufficient_trades",
    ) for index in range(42)}
    report["مشاهدات و غربال پژوهشی"]["قراردادها"] = rows
    return report


SCENARIO = """
<script>
(async () => {
  function check(condition, message) {
    if (!condition) throw new Error(message);
  }
  try {
    const symbol = '<em data-fixture="symbol">نماد & "آزمایشی"</em>';
    const note = '<strong data-fixture="note">توضیح آزمون</strong>';
    const contract = {symbol, note, type: 'CALL', ask: 100, contract_size: 10};
    const responses = [
      {calls: [contract], puts: [{...contract, type: 'PUT'}], count: 2},
      {calls: [], puts: [], count: 0}
    ];
    window.fetch = async url => {
      check(url === '/api/scan' && responses.length, 'unexpected fetch');
      const data = {total: 2, snapshot_age_seconds: 0, ...responses.shift()};
      return {ok: true, json: async () => data};
    };
    await scan();
    for (const [id, side] of [['calls', 'CALL'], ['puts', 'PUT']]) {
      const card = document.querySelector(`#${id} .card`);
      check(card && card.dataset.targetKey === side + ':' + symbol, 'missing card or incorrect key');
      check(card.querySelector('h3').textContent === symbol + ' ' + side, 'symbol not literal');
      check(card.textContent.includes(note), 'note not literal');
      check(!card.querySelector('[data-fixture]'), 'market text became markup');
    }
    await scan();
    for (const id of ['calls', 'puts']) {
      const list = document.getElementById(id);
      check(!list.children.length, 'stale card');
      check(list.textContent === 'قرارداد واجد شرایطی برای نمایش وجود ندارد.', 'missing empty message');
    }
    document.body.dataset.browserCheck = 'passed';
  } catch (error) {
    document.body.dataset.browserCheck = 'failed';
    const diagnostic = document.createElement('pre');
    diagnostic.textContent = error.message;
    document.body.append(diagnostic);
  }
})();
</script>
"""


def check_pilot_503(chrome, scenario):
    """Fetch a real 503 page from the pilot server and render it in Chrome."""
    report, message, guidance = PILOT_SCENARIOS[scenario]
    with TemporaryDirectory() as temporary:
        root = Path(temporary) / "pilot"
        private_directory(root)
        private_directory(root / "reports")
        atomic_json(root / "state.json", load_state(root / "state.json"))
        if report is not None:
            atomic_json(root / "reports" / f"{DAY}.json", report)
        with pilot_server(root) as server:
            status, html = fetch_page(server, "/")
        if status != 503:
            raise SystemExit(f"Pilot 503 ({scenario}) returned {status}")
        page = Path(temporary) / f"pilot-503-{scenario}.html"
        page.write_text(html, encoding="utf-8")
        result = render_chrome(chrome, page, Path(temporary) / f"pilot-profile-{scenario}")
    expected = (message, f"اقدام: {guidance}")
    if result.returncode or any(text not in result.stdout for text in expected):
        raise SystemExit(f"Pilot 503 ({scenario}) browser check failed ({result.returncode}):\n"
                         f"{result.stdout}\n{result.stderr}")
    print(f"PILOT_503_{scenario.upper()}_BROWSER_CHECK passed")


def check_pilot_success(chrome):
    """Check HTTP 200 rendering with search, side, status and pagination filters."""
    cases = (
        ("default", "/", ("browser-00", "browser-01", "صفحهٔ بعد"), ()),
        ("search", "/?q=browser-04&kind=CALL", ("browser-04",), ("browser-06",)),
        ("put", "/?kind=PUT", ("browser-01",), ("browser-00",)),
        ("passed", "/?status=passed", ("browser-00",), ("browser-01",)),
        ("page2", "/?page=2", ("browser-20", "صفحهٔ قبل"), ("browser-00",)),
    )
    with TemporaryDirectory() as temporary:
        root = Path(temporary) / "pilot"
        private_directory(root)
        private_directory(root / "reports")
        atomic_json(root / "state.json", load_state(root / "state.json"))
        atomic_json(root / "reports" / f"{DAY}.json", build_success_report())
        with pilot_server(root) as server:
            for name, path, expected, forbidden in cases:
                status, html = fetch_page(server, path)
                if status != 200:
                    raise SystemExit(f"Pilot success ({name}) returned {status}")
                page = Path(temporary) / f"pilot-200-{name}.html"
                page.write_text(html, encoding="utf-8")
                result = render_chrome(chrome, page, Path(temporary) / f"pilot-profile-{name}")
                missing = [text for text in expected if text not in result.stdout]
                leaked = [text for text in forbidden if text in result.stdout]
                if result.returncode or missing or leaked:
                    raise SystemExit(f"Pilot success ({name}) browser check failed "
                                     f"({result.returncode}, missing={missing}, leaked={leaked}):\n"
                                     f"{result.stdout}\n{result.stderr}")
    print("PILOT_200_FILTERS_BROWSER_CHECK passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", default=shutil.which("google-chrome") or shutil.which("chromium"))
    parser.add_argument("--check", choices=("all", "legacy", "success", "pilot"), default="all")
    args = parser.parse_args()
    if not args.chrome:
        parser.error("Chrome or Chromium is required (or pass --chrome PATH)")
    if args.check in ("all", "legacy"):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            page = root / "dashboard.html"
            fixture = HTML.replace("<head>", '<head><meta http-equiv="Content-Security-Policy" '
                                   'content="connect-src \'none\'">', 1)
            page.write_text(fixture.replace("</body>", SCENARIO + "</body>", 1), encoding="utf-8")
            result = subprocess.run([
                args.chrome, "--headless", "--disable-background-networking", "--disable-component-update",
                "--no-first-run", "--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND",
                f"--user-data-dir={root / 'profile'}", "--dump-dom", page.as_uri(),
            ], capture_output=True, encoding="utf-8", timeout=45)
        if result.returncode or 'data-browser-check="passed"' not in result.stdout:
            tail = result.stdout.rpartition("</script>")[2]
            raise SystemExit(f"Dashboard browser check failed ({result.returncode}):\n{tail}\n{result.stderr}")
        print("LEGACY_DASHBOARD_BROWSER_CHECK passed")
    if args.check in ("all", "success"):
        check_pilot_success(args.chrome)
    if args.check in ("all", "pilot"):
        for scenario in PILOT_SCENARIOS:
            check_pilot_503(args.chrome, scenario)


if __name__ == "__main__":
    main()
