"""
Sphere Automation Worker
Receives: URL + email
Does:     scrape → Claude fills data → build HTML → push to GitHub → email link
"""

import os, re, base64, json, time, requests
from flask import Flask, request, jsonify
from threading import Thread

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
GITHUB_PAT   = os.environ["GITHUB_PAT"]
GITHUB_ORG   = "vrrrtsite-demos"
ANTHROPIC_KEY= os.environ["ANTHROPIC_API_KEY"]
RESEND_KEY   = os.environ.get("RESEND_API_KEY", "")
TEMPLATE_URL = "https://vrrrtsite-demos.github.io/attooh-sphere/"

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "spheres": list_live_spheres()})

@app.route("/build", methods=["POST"])
def build():
    data = request.get_json(force=True)
    url   = data.get("url", "").strip().rstrip("/")
    email = data.get("email", "").strip()

    if not url or not email:
        return jsonify({"error": "url and email required"}), 400

    # Kick off async — respond immediately
    Thread(target=run_pipeline, args=(url, email), daemon=True).start()
    return jsonify({"status": "queued", "url": url, "email": email}), 202

# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline(url: str, email: str):
    try:
        print(f"[pipeline] START {url}")

        # 1. Derive repo slug from domain
        slug = slugify(url)
        repo_name = f"{slug}-sphere"
        print(f"[pipeline] repo: {repo_name}")

        # 2. Fetch template HTML
        template = fetch_template()

        # 3. Use Claude to scrape + generate sphere data JSON
        sphere_data = claude_generate_sphere_data(url)

        # 4. Build HTML by injecting sphere_data into template
        html = build_html(template, sphere_data, url)

        # 5. Push to GitHub
        sphere_url = push_to_github(repo_name, html)

        # 6. Email result
        send_email(email, url, sphere_url)

        print(f"[pipeline] DONE → {sphere_url}")

    except Exception as e:
        print(f"[pipeline] ERROR: {e}")
        send_error_email(email, url, str(e))

# ── Step 2: Fetch template ────────────────────────────────────────────────────

def fetch_template() -> str:
    r = requests.get(TEMPLATE_URL, timeout=30)
    r.raise_for_status()
    return r.text

# ── Step 3: Claude generates sphere data ─────────────────────────────────────

SCRAPE_SYSTEM = """You are a sphere data generator. You will be given a website URL.
Your job:
1. Analyse the website's content based on the URL and domain knowledge
2. Generate a complete sphere data JSON object

Return ONLY valid JSON — no markdown, no explanation, no backticks.

The JSON must have exactly this structure:
{
  "name": "Company Name",
  "slug": "company-name",
  "tagline": "Their tagline or mission statement",
  "base_url": "https://their-domain.com",
  "cta_url": "https://their-domain.com/contact",
  "cta_label": "Get in Touch",
  "primary_color": "#HEXCODE",
  "secondary_color": "#HEXCODE",
  "font": "Arial",
  "strandA": [
    {
      "id": "unique_id",
      "label": "Hub Label",
      "tip": "One sentence about this topic.",
      "children": [
        { "id": "child_id", "label": "Child Label", "tip": "One sentence tip.", "url": "https://real-page-url" },
        { "id": "child_id2", "label": "Child Label 2", "tip": "One sentence tip.", "url": "https://real-page-url" },
        { "id": "child_id3", "label": "Child Label 3", "tip": "One sentence tip.", "url": "https://real-page-url" }
      ]
    }
  ],
  "strandB": [ ... same structure as strandA ... ],
  "content": {
    "hub_id": {
      "title": "Hub Title",
      "color": "#HEXCODE",
      "items": [
        {
          "id": "child_id",
          "icon": "emoji",
          "title": "Item Title",
          "body": "2-3 sentences of real educational content about this topic.",
          "articles": [{ "title": "Link text", "url": "https://real-url" }]
        }
      ]
    }
  },
  "paths": [
    { "key": "path1", "label": "emoji Path Label 1" },
    { "key": "path2", "label": "emoji Path Label 2" },
    { "key": "path3", "label": "emoji Path Label 3" },
    { "key": "path4", "label": "emoji Path Label 4" },
    { "key": "path5", "label": "emoji Path Label 5" }
  ]
}

Rules:
- strandA: 8 hubs, each with exactly 3 children — represent the company's PRODUCTS/SERVICES/TOPICS
- strandB: 8 hubs, each with exactly 3 children — represent IDENTITY/VALUES/STORY/ABOUT
- content: one entry per hub id (all 16 hubs from both strands)
- All URLs must be real pages on the client's domain
- primary_color: pick a strong brand color (teal, blue, green, red, etc. — avoid white/grey)
- secondary_color: a complementary darker or contrasting color
- All text must be specific to this company — no generic filler
"""

def claude_generate_sphere_data(url: str) -> dict:
    prompt = f"""Generate complete sphere data JSON for this website: {url}

Research the company from the URL. Use your knowledge of this company/domain to fill in accurate, specific content.
Return only the JSON object."""

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 8000,
            "system": SCRAPE_SYSTEM,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=120
    )
    resp.raise_for_status()
    raw = resp.json()["content"][0]["text"]

    # Strip any accidental markdown fences
    raw = re.sub(r'^```(?:json)?\s*', '', raw.strip())
    raw = re.sub(r'\s*```$', '', raw.strip())

    return json.loads(raw)

# ── Step 4: Build HTML ────────────────────────────────────────────────────────

def build_html(template: str, d: dict, original_url: str) -> str:
    html = template

    # BASE and CTA
    html = re.sub(
        r'const BASE\s*=\s*"[^"]*";',
        f'const BASE = "{d["base_url"]}";',
        html
    )
    html = re.sub(
        r'const CTA\s*=\s*"[^"]*";',
        f'const CTA = "{d["cta_url"]}";',
        html
    )

    # strandA
    strand_a_js = json_to_strand_js(d["strandA"], "A")
    html = re.sub(
        r'const strandA = \[[\s\S]*?\];\s*\n(?=const strandB)',
        f'const strandA = {strand_a_js};\n\n',
        html
    )

    # strandB
    strand_b_js = json_to_strand_js(d["strandB"], "B")
    html = re.sub(
        r'const strandB = \[[\s\S]*?\];\s*\n(?=const rungHubs)',
        f'const strandB = {strand_b_js};\n\n',
        html
    )

    # CONTENT
    content_js = build_content_js(d["content"])
    html = re.sub(
        r'const ATTOOH_CONTENT = \{[\s\S]*?\};\s*\n(?=// ── BUILD NAV BAR)',
        f'const ATTOOH_CONTENT = {content_js};\n\n',
        html
    )

    # pathDefs
    paths_js = json.dumps(d["paths"], indent=2)
    html = re.sub(
        r'const pathDefs = \[[\s\S]*?\];',
        f'const pathDefs = {paths_js};',
        html
    )

    # Colors — replace attooh green palette with client colors
    primary   = d["primary_color"].lstrip("#")
    secondary = d["secondary_color"].lstrip("#")
    pr, pg, pb = int(primary[0:2],16), int(primary[2:4],16), int(primary[4:6],16)
    sr, sg, sb = int(secondary[0:2],16), int(secondary[2:4],16), int(secondary[4:6],16)

    color_map = [
        ("#9cd31e", f"#{primary}"),
        ("#7aaa10", darken_hex(primary, 0.85)),
        ("#3a6000", f"#{secondary}"),
        ("#1a3000", darken_hex(secondary, 0.80)),
        ("#507a00", darken_hex(primary, 0.70)),
        ("rgba(156,211,30", f"rgba({pr},{pg},{pb}"),
        ("rgba(58,96,0",    f"rgba({sr},{sg},{sb}"),
        ("0x9cd31e",        f"0x{primary}"),
        ("0x3a6000",        f"0x{secondary}"),
    ]
    for old, new in color_map:
        html = html.replace(old, new)

    # Font
    font = d.get("font", "Arial")
    html = html.replace("'Nunito', sans-serif", f"'{font}', sans-serif")
    html = html.replace('"Nunito", sans-serif',  f"'{font}', sans-serif")
    html = re.sub(r'<link[^>]*fonts\.googleapis[^>]*>\n?', '', html)

    # Title + branding
    company = d["name"]
    tagline = d.get("tagline", "Knowledge Sphere")
    html = html.replace(
        "attooh! — Financial Services Knowledge Sphere",
        f"{company} — {tagline} Knowledge Sphere"
    )
    html = html.replace("attooh!", company)
    html = html.replace(
        'data-sphere-id="attooh-sphere"',
        f'data-sphere-id="{d["slug"]}-sphere"'
    )
    html = html.replace("☕ Book a Meeting", d.get("cta_label", "Get in Touch"))

    # Verify forEach not dropped
    if "pathDefs.forEach" not in html:
        html = html.replace(
            "];\nconst p = document.createElement",
            "];\npathDefs.forEach(({ key, label }) => {\nconst p = document.createElement"
        )

    return html

def json_to_strand_js(strand: list, strand_id: str) -> str:
    lines = ["["]
    for hub in strand:
        children_js = []
        for c in hub["children"]:
            url_part = f', url: "{c["url"]}"' if c.get("url") else ""
            children_js.append(
                f'      {{ id: "{c["id"]}", label: "{c["label"]}", tip: "{esc(c["tip"])}"{url_part} }}'
            )
        children_str = ",\n".join(children_js)
        lines.append(f'''  {{
    id: "{hub["id"]}", label: "{hub["label"]}", strand: "{strand_id}",
    tip: "{esc(hub["tip"])}",
    children: [
{children_str}
    ]
  }},''')
    lines.append("]")
    return "\n".join(lines)

def build_content_js(content: dict) -> str:
    lines = ["{"]
    for hub_id, hub_data in content.items():
        items_js = []
        for item in hub_data["items"]:
            articles = ", ".join(
                f'{{ title: "{esc(a["title"])}", url: "{a["url"]}" }}'
                for a in item.get("articles", [])
            )
            items_js.append(
                f'    {{ id: "{item["id"]}", icon: "{item["icon"]}", '
                f'title: "{esc(item["title"])}", body: "{esc(item["body"])}", '
                f'articles: [{articles}] }}'
            )
        items_str = ",\n".join(items_js)
        color = hub_data.get("color", "#00AFAD")
        lines.append(
            f'  {hub_id}: {{ title: "{esc(hub_data["title"])}", color: "{color}", items: [\n{items_str}\n  ]}},')
    lines.append("}")
    return "\n".join(lines)

def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

def darken_hex(hex_str: str, factor: float) -> str:
    r = int(int(hex_str[0:2], 16) * factor)
    g = int(int(hex_str[2:4], 16) * factor)
    b = int(int(hex_str[4:6], 16) * factor)
    return f"#{r:02X}{g:02X}{b:02X}"

# ── Step 5: Push to GitHub ────────────────────────────────────────────────────

def push_to_github(repo_name: str, html: str) -> str:
    headers = {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github.v3+json"
    }

    # Create repo (ignore if exists)
    requests.post(
        f"https://api.github.com/orgs/{GITHUB_ORG}/repos",
        headers=headers,
        json={"name": repo_name, "private": False, "auto_init": False}
    )
    time.sleep(2)

    # Get existing SHA if file exists
    sha = None
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_ORG}/{repo_name}/contents/index.html",
        headers=headers
    )
    if r.status_code == 200:
        sha = r.json().get("sha")

    # Push file
    payload = {
        "message": f"deploy: {repo_name}",
        "content": base64.b64encode(html.encode()).decode()
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(
        f"https://api.github.com/repos/{GITHUB_ORG}/{repo_name}/contents/index.html",
        headers=headers,
        json=payload
    )
    r.raise_for_status()

    # Enable Pages (ignore if already on)
    requests.post(
        f"https://api.github.com/repos/{GITHUB_ORG}/{repo_name}/pages",
        headers=headers,
        json={"source": {"branch": "main", "path": "/"}}
    )

    return f"https://{GITHUB_ORG}.github.io/{repo_name}/"

# ── Step 6: Email ─────────────────────────────────────────────────────────────

def send_email(to: str, source_url: str, sphere_url: str):
    if not RESEND_KEY:
        print(f"[email] No RESEND_KEY — would send sphere URL {sphere_url} to {to}")
        return

    requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json"},
        json={
            "from":    "spheres@vrrrt.com",
            "to":      [to],
            "subject": f"Your Knowledge Sphere is live! 🌐",
            "html":    f"""
<h2>Your sphere is live!</h2>
<p>We built a Knowledge Sphere for <strong>{source_url}</strong>.</p>
<p><a href="{sphere_url}" style="font-size:18px;font-weight:bold;">👉 View your sphere</a></p>
<p>Share it with anyone — it's public and works on mobile.</p>
<br>
<p>— The Vrrrt! team</p>
"""
        }
    )

def send_error_email(to: str, source_url: str, error: str):
    if not RESEND_KEY:
        print(f"[email] ERROR — {error}")
        return
    requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json"},
        json={
            "from":    "spheres@vrrrt.com",
            "to":      [to],
            "subject": "Sphere build issue — we're looking into it",
            "html":    f"<p>We hit an issue building the sphere for {source_url}. Our team has been notified. Error: {error}</p>"
        }
    )

# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(url: str) -> str:
    domain = re.sub(r'^https?://(www\.)?', '', url).split('/')[0]
    base = domain.split('.')[0]
    return re.sub(r'[^a-z0-9]', '-', base.lower()).strip('-')

def list_live_spheres():
    headers = {"Authorization": f"Bearer {GITHUB_PAT}"}
    r = requests.get(f"https://api.github.com/orgs/{GITHUB_ORG}/repos?per_page=50", headers=headers)
    if r.status_code == 200:
        return [f"https://{GITHUB_ORG}.github.io/{repo['name']}/" for repo in r.json() if repo["name"].endswith("-sphere")]
    return []

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
