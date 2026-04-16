"""
Sphere Automation Worker — with /status debug endpoint
"""

import os, re, base64, json, time, requests, traceback
from flask import Flask, request, jsonify
from threading import Thread

app = Flask(__name__)

GITHUB_PAT    = os.environ.get("GITHUB_PAT", "")
GITHUB_ORG    = "vrrrtsite-demos"
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RESEND_KEY    = os.environ.get("RESEND_API_KEY", "")
TEMPLATE_URL  = "https://vrrrtsite-demos.github.io/attooh-sphere/"

# Job log for debug
jobs = {}

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "spheres": list_live_spheres(),
                    "env": {"github_pat": bool(GITHUB_PAT), "anthropic": bool(ANTHROPIC_KEY)}})

@app.route("/status", methods=["GET"])
def status():
    return jsonify({"jobs": jobs})

@app.route("/build", methods=["POST"])
def build():
    data  = request.get_json(force=True)
    url   = data.get("url", "").strip().rstrip("/")
    email = data.get("email", "").strip()
    if not url or not email:
        return jsonify({"error": "url and email required"}), 400
    job_id = f"{slugify(url)}-{int(time.time())}"
    jobs[job_id] = {"status": "queued", "url": url}
    Thread(target=run_pipeline, args=(url, email, job_id), daemon=True).start()
    return jsonify({"status": "queued", "url": url, "email": email, "job_id": job_id}), 202

def run_pipeline(url, email, job_id):
    try:
        jobs[job_id]["status"] = "fetching_template"
        template = fetch_template()

        jobs[job_id]["status"] = "calling_claude"
        sphere_data = claude_generate_sphere_data(url)

        jobs[job_id]["status"] = "building_html"
        slug = sphere_data.get("slug", slugify(url))
        repo_name = f"{slug}-sphere"
        html = build_html(template, sphere_data, url)

        jobs[job_id]["status"] = "pushing_to_github"
        sphere_url = push_to_github(repo_name, html)

        jobs[job_id]["status"] = "done"
        jobs[job_id]["sphere_url"] = sphere_url
        send_email(email, url, sphere_url)
        print(f"[pipeline] DONE → {sphere_url}")

    except Exception as e:
        tb = traceback.format_exc()
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["traceback"] = tb
        print(f"[pipeline] ERROR: {e}\n{tb}")
        send_error_email(email, url, str(e))

def fetch_template():
    r = requests.get(TEMPLATE_URL, timeout=30)
    r.raise_for_status()
    return r.text

SCRAPE_SYSTEM = """You are a sphere data generator. Given a website URL, generate complete sphere data JSON.

Return ONLY valid JSON — no markdown, no explanation, no backticks.

Structure:
{
  "name": "Company Name",
  "slug": "company-name",
  "tagline": "Their tagline",
  "base_url": "https://their-domain.com",
  "cta_url": "https://their-domain.com/contact",
  "cta_label": "Get in Touch",
  "primary_color": "#HEXCODE",
  "secondary_color": "#HEXCODE",
  "font": "Arial",
  "strandA": [
    {
      "id": "unique_id", "label": "Hub Label", "tip": "One sentence.",
      "children": [
        {"id": "child1", "label": "Child", "tip": "One sentence.", "url": "https://real-url"},
        {"id": "child2", "label": "Child", "tip": "One sentence.", "url": "https://real-url"},
        {"id": "child3", "label": "Child", "tip": "One sentence.", "url": "https://real-url"}
      ]
    }
  ],
  "strandB": [same structure],
  "content": {
    "hub_id": {
      "title": "Hub Title", "color": "#HEXCODE",
      "items": [
        {"id": "child_id", "icon": "emoji", "title": "Title", "body": "2-3 sentences.", "articles": [{"title": "Link", "url": "https://url"}]}
      ]
    }
  },
  "paths": [
    {"key": "path1", "label": "emoji Label"},
    {"key": "path2", "label": "emoji Label"},
    {"key": "path3", "label": "emoji Label"},
    {"key": "path4", "label": "emoji Label"},
    {"key": "path5", "label": "emoji Label"}
  ]
}

Rules: strandA=8 hubs (products/services), strandB=8 hubs (identity/about), each hub has exactly 3 children, content has entry for every hub id, paths keys must match pathDef keys and hub ids in paths.hubs must exist in strandA/strandB ids."""

def claude_generate_sphere_data(url):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": "claude-sonnet-4-20250514", "max_tokens": 8000, "system": SCRAPE_SYSTEM,
              "messages": [{"role": "user", "content": f"Generate sphere data JSON for: {url}"}]},
        timeout=120
    )
    resp.raise_for_status()
    raw = resp.json()["content"][0]["text"].strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw.strip())
    return json.loads(raw)

def build_html(template, d, original_url):
    html = template
    html = re.sub(r'const BASE\s*=\s*"[^"]*";', f'const BASE = "{d["base_url"]}";', html)
    html = re.sub(r'const CTA\s*=\s*"[^"]*";',  f'const CTA = "{d["cta_url"]}";',  html)

    strand_a_js = strands_to_js(d["strandA"], "A")
    html = re.sub(r'const strandA = \[[\s\S]*?\];\s*\n(?=const strandB)', f'const strandA = {strand_a_js};\n\n', html)

    strand_b_js = strands_to_js(d["strandB"], "B")
    html = re.sub(r'const strandB = \[[\s\S]*?\];\s*\n(?=const rungHubs)', f'const strandB = {strand_b_js};\n\n', html)

    content_js = content_to_js(d["content"])
    html = re.sub(r'const ATTOOH_CONTENT = \{[\s\S]*?\};\s*\n(?=// ── BUILD NAV BAR)', f'const ATTOOH_CONTENT = {content_js};\n\n', html)

    paths_js = json.dumps(d["paths"], indent=2)
    html = re.sub(r'const pathDefs = \[[\s\S]*?\];', f'const pathDefs = {paths_js};', html)

    # Build PATHS constant from paths + strandA/B ids
    hub_ids_a = [h["id"] for h in d["strandA"]]
    hub_ids_b = [h["id"] for h in d["strandB"]]
    all_hub_ids = hub_ids_a + hub_ids_b
    paths_const = build_paths_const(d["paths"], all_hub_ids, d["primary_color"], d["secondary_color"])
    html = re.sub(r'const PATHS = \{[\s\S]*?\};', paths_const, html)

    # Colors
    p = d["primary_color"].lstrip("#")
    s = d["secondary_color"].lstrip("#")
    pr,pg,pb = int(p[0:2],16), int(p[2:4],16), int(p[4:6],16)
    sr,sg,sb = int(s[0:2],16), int(s[2:4],16), int(s[4:6],16)
    for old, new in [
        ("#9cd31e",f"#{p}"),("#7aaa10",darken(p,.85)),("#3a6000",f"#{s}"),
        ("#1a3000",darken(s,.80)),("#507a00",darken(p,.70)),
        ("rgba(156,211,30",f"rgba({pr},{pg},{pb}"),("rgba(58,96,0",f"rgba({sr},{sg},{sb}"),
        ("0x9cd31e",f"0x{p}"),("0x3a6000",f"0x{s}")
    ]: html = html.replace(old, new)

    # CSS pill active colors
    paths = d["paths"]
    colors = [d["primary_color"], darken(p,.80), darken(p,.65), d["secondary_color"], darken(s,.80)]
    css_rules = "\n".join([
        f'  .nav-pill.path[data-path="{paths[i]["key"]}"].active {{ background: {colors[i]}; color: #fff; }}'
        for i in range(min(5, len(paths)))
    ])
    html = re.sub(
        r'\.nav-pill\.path\[data-path="protect"\][\s\S]*?\.nav-pill\.path\[data-path="discover"\].*?\}',
        css_rules, html
    )

    font = d.get("font", "Arial")
    html = html.replace("'Nunito', sans-serif", f"'{font}', sans-serif")
    html = re.sub(r'<link[^>]*fonts\.googleapis[^>]*>\n?', '', html)

    company = d["name"]
    html = html.replace("attooh! — Financial Services Knowledge Sphere", f"{company} — {d.get('tagline','Knowledge')} Knowledge Sphere")
    html = html.replace("attooh!", company)
    html = html.replace('data-sphere-id="attooh-sphere"', f'data-sphere-id="{d["slug"]}-sphere"')
    html = html.replace("☕ Book a Meeting", d.get("cta_label", "Get in Touch"))

    if "pathDefs.forEach" not in html:
        html = html.replace("];\nconst p = document.createElement", "];\npathDefs.forEach(({ key, label }) => {\nconst p = document.createElement")

    return html

def build_paths_const(paths, all_hub_ids, primary, secondary):
    colors = [primary, darken(primary.lstrip("#"),.85), darken(primary.lstrip("#"),.70), secondary, darken(secondary.lstrip("#"),.80)]
    n = len(all_hub_ids)
    chunk = max(1, n // len(paths))
    lines = ["const PATHS = {"]
    for i, path in enumerate(paths):
        start = (i * chunk) % n
        hubs = all_hub_ids[start:start+4] or all_hub_ids[:4]
        color = colors[i % len(colors)]
        lines.append(f'  {path["key"]}: {{ label: "{path["label"]}", color: "{color}", textColor: "#fff", hubs: {json.dumps(hubs)} }},')
    lines.append("};")
    return "\n".join(lines)

def strands_to_js(strand, sid):
    lines = ["["]
    for hub in strand:
        children = ",\n".join([
            f'      {{ id: "{c["id"]}", label: "{esc(c["label"])}", tip: "{esc(c["tip"])}"{", url: " + json.dumps(c["url"]) if c.get("url") else ""} }}'
            for c in hub["children"]
        ])
        lines.append(f'  {{\n    id: "{hub["id"]}", label: "{esc(hub["label"])}", strand: "{sid}",\n    tip: "{esc(hub["tip"])}",\n    children: [\n{children}\n    ]\n  }},')
    lines.append("]")
    return "\n".join(lines)

def content_to_js(content):
    lines = ["{"]
    for hid, hdata in content.items():
        items = ",\n".join([
            f'    {{ id: "{it["id"]}", icon: "{it.get("icon","📌")}", title: "{esc(it["title"])}", body: "{esc(it["body"])}", articles: [{", ".join([\'{ title: "\' + esc(a["title"]) + \'", url: "\' + a["url"] + \'" }\' for a in it.get("articles",[])])}] }}'
            for it in hdata["items"]
        ])
        lines.append(f'  {hid}: {{ title: "{esc(hdata["title"])}", color: "{hdata.get("color","#00AFAD")}", items: [\n{items}\n  ]}},')
    lines.append("}")
    return "\n".join(lines)

def esc(s): return str(s).replace("\\","\\\\").replace('"','\\"').replace("\n"," ")
def darken(h, f): h=h.lstrip("#"); return f"#{int(int(h[0:2],16)*f):02X}{int(int(h[2:4],16)*f):02X}{int(int(h[4:6],16)*f):02X}"

def push_to_github(repo_name, html):
    headers = {"Authorization": f"Bearer {GITHUB_PAT}", "Accept": "application/vnd.github.v3+json"}
    requests.post(f"https://api.github.com/orgs/{GITHUB_ORG}/repos", headers=headers,
                  json={"name": repo_name, "private": False, "auto_init": False})
    time.sleep(2)
    sha = None
    r = requests.get(f"https://api.github.com/repos/{GITHUB_ORG}/{repo_name}/contents/index.html", headers=headers)
    if r.status_code == 200: sha = r.json().get("sha")
    payload = {"message": f"deploy: {repo_name}", "content": base64.b64encode(html.encode()).decode()}
    if sha: payload["sha"] = sha
    r = requests.put(f"https://api.github.com/repos/{GITHUB_ORG}/{repo_name}/contents/index.html", headers=headers, json=payload)
    r.raise_for_status()
    requests.post(f"https://api.github.com/repos/{GITHUB_ORG}/{repo_name}/pages", headers=headers,
                  json={"source": {"branch": "main", "path": "/"}})
    return f"https://{GITHUB_ORG}.github.io/{repo_name}/"

def send_email(to, source_url, sphere_url):
    if not RESEND_KEY: print(f"[email] sphere ready: {sphere_url} → {to}"); return
    requests.post("https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json"},
        json={"from": "spheres@vrrrt.com", "to": [to], "subject": "Your Knowledge Sphere is live! 🌐",
              "html": f'<h2>Your sphere is live!</h2><p><a href="{sphere_url}">👉 View your sphere</a></p>'})

def send_error_email(to, source_url, error):
    if not RESEND_KEY: return
    requests.post("https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json"},
        json={"from": "spheres@vrrrt.com", "to": [to], "subject": "Sphere build issue",
              "html": f"<p>Issue building sphere for {source_url}. Error: {error}</p>"})

def slugify(url): 
    domain = re.sub(r'^https?://(www\.)?','',url).split('/')[0]
    return re.sub(r'[^a-z0-9]','-',domain.split('.')[0].lower()).strip('-')

def list_live_spheres():
    if not GITHUB_PAT: return []
    r = requests.get(f"https://api.github.com/orgs/{GITHUB_ORG}/repos?per_page=50",
                     headers={"Authorization": f"Bearer {GITHUB_PAT}"})
    if r.status_code == 200:
        return [f"https://{GITHUB_ORG}.github.io/{repo['name']}/" for repo in r.json() if repo["name"].endswith("-sphere")]
    return []

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
