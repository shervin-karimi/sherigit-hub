"""Exercise the legacy dashboard and pilot 503 pages in headless Chrome."""

import argparse
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
        server = PilotServer(root, 0, clock=pilot_clock)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            client = HTTPConnection(HOST, server.server_port, timeout=5)
            try:
                client.request("GET", "/")
                response = client.getresponse()
                status, html = response.status, response.read().decode("utf-8")
            finally:
                client.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
        if status != 503:
            raise SystemExit(f"Pilot 503 ({scenario}) returned {status}")
        page = Path(temporary) / f"pilot-503-{scenario}.html"
        page.write_text(html, encoding="utf-8")
        result = subprocess.run([
            chrome, "--headless", "--disable-background-networking", "--disable-component-update",
            "--no-first-run", "--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND",
            f"--user-data-dir={Path(temporary) / f'pilot-profile-{scenario}'}", "--dump-dom", page.as_uri(),
        ], capture_output=True, encoding="utf-8", timeout=45)
    expected = (message, f"اقدام: {guidance}")
    if result.returncode or any(text not in result.stdout for text in expected):
        raise SystemExit(f"Pilot 503 ({scenario}) browser check failed ({result.returncode}):\n"
                         f"{result.stdout}\n{result.stderr}")
    print(f"PILOT_503_{scenario.upper()}_BROWSER_CHECK passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", default=shutil.which("google-chrome") or shutil.which("chromium"))
    parser.add_argument("--check", choices=("all", "legacy", "pilot"), default="all")
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
    if args.check in ("all", "pilot"):
        for scenario in PILOT_SCENARIOS:
            check_pilot_503(args.chrome, scenario)


if __name__ == "__main__":
    main()
