import base64
import concurrent.futures
import csv
import datetime
import hashlib
import html
import json
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).parent
BASE = "https://leaderboard.pasit.site/"
RAW = ROOT / "original"
RAW.mkdir(exist_ok=True)
manifest = {}


def fetch(path):
    url = path if path.startswith("https:") else BASE + path
    with tempfile.TemporaryDirectory() as tmp:
        headers = Path(tmp) / "headers"
        content = subprocess.check_output(
            [
                "curl.exe",
                "-L",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "45",
                "-D",
                str(headers),
                url,
            ]
        )
        header_text = headers.read_text()
        match = re.findall(r"(?im)^content-type:\s*([^;\r\n]+)", header_text)
        mime = match[-1] if match else "application/octet-stream"
        match = re.findall(r"(?im)^timestamp:\s*([^\r\n]+)", header_text)
        timestamp = match[-1] if match else None
    target = RAW / (
        "index.html"
        if not path
        else path.rstrip("/") + ("/index.json" if path.endswith("/") else "")
    )
    if path.startswith("https:"):
        target = RAW / "lib/jquery.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    manifest[path] = {
        "url": url,
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "timestamp": timestamp,
    }
    return content, mime


def script(source):
    return "<script>" + source.replace("</script", "<\\/script") + "</script>"


started = datetime.datetime.now(datetime.timezone.utc).isoformat()
page = fetch("")[0].decode()
scripts = re.findall(r'<script src="([^"]+)"', page)
scripts = [p for p in scripts if not p.startswith("//")]
paths = scripts + [
    "Ranking.css",
    "logo",
    "img/favicon.ico",
    "img/close.png",
    "img/tick_black.png",
    "img/tick_white.png",
    "https://ajax.googleapis.com/ajax/libs/jquery/3.6.0/jquery.min.js",
    "contests/",
    "tasks/",
    "teams/",
    "users/",
    "scores",
    "history",
    "config",
]
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
    assets = dict(zip(paths, pool.map(fetch, paths)))
data = {
    p: json.loads(assets[p][0])
    for p in ["contests/", "tasks/", "teams/", "users/", "scores", "history", "config"]
}
assert data["users/"] and data["tasks/"] and data["scores"]
assert set(data["scores"]) <= set(data["users/"])
extra = (
    ["sublist/" + u for u in data["users/"]]
    + ["flags/" + t for t in data["teams/"]]
    + ["faces/" + u for u in data["users/"]]
)
failures = []


def optional(path):
    try:
        return path, fetch(path)
    except Exception as error:
        failures.append({"path": path, "error": str(error)})
        return path, None


with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
    for path, result in pool.map(optional, extra):
        if result:
            assets[path] = result
            if path.startswith("sublist/"):
                data[path] = json.loads(result[0])

images = {
    p: "data:" + mime + ";base64," + base64.b64encode(content).decode()
    for p, (content, mime) in assets.items()
    if mime.startswith("image/") or p.startswith(("flags/", "faces/")) or p == "logo"
}
css = assets["Ranking.css"][0].decode()
css = re.sub(
    r"url\(['\"]?([^)'\"]+)['\"]?\)", lambda m: 'url("' + images[m[1]] + '")', css
)
# Retain the original UI, but answer its requests from the captured data.
bridge = (
    "var archiveData="
    + json.dumps(data)
    + ";var archiveImages="
    + json.dumps(images)
    + ";"
)
bridge += """$.ajax=function(options){
  var key=options.url;
  if (!(key in archiveData)) { if(options.error) options.error(); return; }
  var done=function(){if(options.success) options.success(JSON.parse(JSON.stringify(archiveData[key])), 'success', {getResponseHeader:function(){return '0';}});};
  if(options.async===false) done(); else setTimeout(done,0);
};"""
body = page[page.index("<body") : page.rindex("</body>")]
body = re.sub(r"<script>.*?</script>", "", body, flags=re.S)
body = re.sub(
    r'<a href="https://leaderboard.pasit.site/cdn-cgi/.*?</a>', "", body, flags=re.S
)
body = re.sub(r'src="([^"]+)"', lambda m: 'src="' + images.get(m[1], m[1]) + '"', body)
body = body.replace(
    '<div id="ConnectionStatus_label">Status</div>',
    '<div id="ConnectionStatus_label">Archived</div>',
)
css += '#ConnectionStatus_box[data-status="archived"] #ConnectionStatus_label, #ConnectionStatus_box[data-status="archived"] #ConnectionStatus_text { background-color: #1677c8; color: white; border-color: #1677c8; }'
overrides = """DataStore.create_event_source=function(){
  $('#ConnectionStatus_box').attr('data-status','archived');
  $('#ConnectionStatus_text').text('17 October 2026');
};
Config.get_face_url=function(k){return archiveImages['faces/'+k] || '';};
Config.get_flag_url=function(k){return archiveImages['flags/'+k] || '';};
"""
output = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Leaderboard archive — 17 October 2026</title><style>'
    + css
    + "</style>"
)
output += script(
    assets["https://ajax.googleapis.com/ajax/libs/jquery/3.6.0/jquery.min.js"][
        0
    ].decode()
) + script(bridge)
for p in scripts:
    if p == "Ranking.js":
        output += script(overrides)
    output += script(assets[p][0].decode())
output += "</head>" + body + "</body></html>"
(ROOT / "leaderboard.html").write_text(output, encoding="utf-8")
(ROOT / "data.json").write_text(
    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
)
tasks = sorted(
    data["tasks/"],
    key=lambda t: (
        data["contests/"][data["tasks/"][t]["contest"]]["begin"],
        data["tasks/"][t]["order"],
        t,
    ),
)
users = sorted(
    data["users/"], key=lambda u: (-sum(data["scores"].get(u, {}).values()), u)
)
with (ROOT / "standings.csv").open("w", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["Rank", "ID", "First name", "Last name", "Team", "Total"] + tasks)
    totals = {u: sum(data["scores"].get(u, {}).values()) for u in users}
    for u in users:
        user = data["users/"][u]
        writer.writerow(
            [
                1 + sum(v > totals[u] for v in totals.values()),
                u,
                user["f_name"],
                user["l_name"],
                user["team"],
                totals[u],
            ]
            + [data["scores"].get(u, {}).get(t, 0) for t in tasks]
        )
(ROOT / "manifest.json").write_text(
    json.dumps(
        {
            "source": BASE,
            "capture_started_utc": started,
            "capture_finished_utc": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
            "files": manifest,
            "unavailable_optional_resources": failures,
        },
        indent=2,
    ),
    encoding="utf-8",
)
(ROOT / "README.txt").write_text(
    "Leaderboard archive: https://leaderboard.pasit.site/\nCaptured 17 October 2026 (Asia/Singapore).\nOpen leaderboard.html directly in a browser; no server or internet required.\nstandings.csv contains combined standings; data.json contains captured public data.\noriginal/ contains downloaded source responses; manifest.json records hashes and unavailable resources.\nCapture requests are sequential/parallel over a short interval, not an atomic server backup.\n",
    encoding="utf-8",
)
assert (
    len(list(csv.reader((ROOT / "standings.csv").open(encoding="utf-8-sig"))))
    == len(users) + 1
)
with zipfile.ZipFile(
    ROOT.with_name(ROOT.name + ".zip"), "w", zipfile.ZIP_DEFLATED
) as z:
    for file in ROOT.rglob("*"):
        if file.is_file():
            z.write(file, file.relative_to(ROOT))
print(
    json.dumps(
        {
            "users": len(users),
            "tasks": len(tasks),
            "contests": len(data["contests/"]),
            "history_events": len(data["history"]),
            "unavailable_optional_resources": len(failures),
            "html_bytes": len(output.encode()),
            "zip": str(ROOT.with_name(ROOT.name + ".zip")),
        }
    )
)
