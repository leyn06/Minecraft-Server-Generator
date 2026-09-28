#!/usr/bin/env python3
"""Modpack Importer web UI for Umbrel.

Stdlib-only web app that imports Minecraft modpacks, mods, plugins and
maps from Modrinth, CurseForge and SpigotMC into Crafty Controller.

It writes request files into /queue; the itzg/minecraft-server workers
consume them and drop the finished servers into /out and Crafty's import
folder.
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

QUEUE_DIR = os.environ.get("QUEUE_DIR", "/queue")
OUT_DIR = os.environ.get("OUT_DIR", "/out")
LOGS_DIR = os.environ.get("LOGS_DIR", "/out-logs")
ALLOWED_JAVA = ("java21", "java17")
ALLOWED_LOADERS = ("forge", "fabric", "quilt")
MODRINTH_API = "https://api.modrinth.com/v2"
UA = {"User-Agent": "umbrel-modpack-importer/1.0 (leyn06)"}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def sanitize_name(value):
    value = value.lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value).strip("-.")
    return value[:60] or "modpack"


def shell_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def modrinth_slug_list(raw):
    """Accept slugs or modrinth.com/mod|plugin/<slug> URLs, comma/newline separated."""
    slugs = []
    for item in re.split(r"[,\n]", raw or ""):
        item = item.strip()
        if not item:
            continue
        m = re.search(r"modrinth\.com/(?:mod|plugin)/([A-Za-z0-9-]+)", item)
        if m:
            item = m.group(1)
        if re.fullmatch(r"[A-Za-z0-9-]+", item):
            slugs.append(item)
    return slugs


def spiget_id_list(raw):
    ids = []
    for item in re.split(r"[,\n]", raw or ""):
        m = re.search(r"(?:resources/|resource/)?(\d+)", item.strip())
        if m:
            ids.append(m.group(1))
    return ids


def parse_source(source):
    """Return (mode, name, env-dict) from a modpack URL or Modrinth slug."""
    source = source.strip()

    m = re.search(
        r"https?://modrinth\.com/modpack/([A-Za-z0-9-]+)(?:/version/([A-Za-z0-9._-]+))?",
        source,
    )
    if m:
        slug, version = m.group(1), m.group(2)
        env = {"MODPACK_PLATFORM": "modrinth"}
        if version:
            env["MODRINTH_MODPACK"] = source
            name = f"{slug}-{version}"
        else:
            env["MODRINTH_MODPACK"] = slug
            name = slug
        return "modrinth", sanitize_name(name), env

    m = re.search(
        r"https?://www\.curseforge\.com/minecraft/modpacks/([A-Za-z0-9-]+)(?:/files/(\d+))?",
        source,
    )
    if m:
        env = {"MODPACK_PLATFORM": "auto_curseforge", "CF_PAGE_URL": source}
        name = m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")
        return "curseforge", sanitize_name(name), env

    if re.fullmatch(r"[A-Za-z0-9-]+", source):
        return "modrinth", sanitize_name(source), {
            "MODPACK_PLATFORM": "modrinth",
            "MODRINTH_MODPACK": source,
        }

    raise ValueError(
        "Lien non reconnu. Colle une page Modrinth ou CurseForge, ou un slug Modrinth."
    )


def modrinth_project_exists(slug):
    try:
        req = urllib.request.Request(f"{MODRINTH_API}/project/{slug}", headers=UA)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def modrinth_search(query):
    params = urllib.parse.urlencode(
        {
            "query": query,
            "limit": 12,
            "facets": json.dumps([["project_type:modpack"]]),
        }
    )
    req = urllib.request.Request(f"{MODRINTH_API}/search?{params}", headers=UA)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    hits = []
    for h in data.get("hits", []):
        hits.append(
            {
                "slug": h.get("slug", ""),
                "title": h.get("title", ""),
                "description": (h.get("description") or "")[:140],
                "icon_url": h.get("icon_url", ""),
                "downloads": h.get("downloads", 0),
                "versions": (h.get("versions") or [])[:4],
            }
        )
    return hits


def write_request(java, name, env):
    os.makedirs(QUEUE_DIR, exist_ok=True)
    filename = f"{java}-{int(time.time())}-{name}.env"
    lines = [f"NAME={shell_quote(name)}"]
    for key, value in env.items():
        lines.append(f"{key}={shell_quote(value)}")
    tmp = os.path.join(QUEUE_DIR, f".{filename}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, os.path.join(QUEUE_DIR, filename))


def status_payload():
    status = {"state": "idle", "name": ""}
    try:
        with open(os.path.join(OUT_DIR, "status.json"), encoding="utf-8") as fh:
            status.update(json.load(fh))
    except Exception:
        pass
    pending = []
    try:
        pending = sorted(f for f in os.listdir(QUEUE_DIR) if f.endswith(".env"))
    except Exception:
        pass
    imports = []
    try:
        for entry in sorted(os.listdir(OUT_DIR), reverse=True):
            if entry in ("status.json",) or entry.startswith("."):
                continue
            path = os.path.join(OUT_DIR, entry)
            if os.path.isdir(path):
                imports.append(entry)
    except Exception:
        pass
    return {"status": status, "pending": pending, "imports": imports}


# --------------------------------------------------------------------------
# HTML UI (French, single page)
# --------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Modpack Importer</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #0d1117; color: #e6edf3; padding-bottom: 3rem;
  }
  .wrap { max-width: 720px; margin: 0 auto; padding: 0 1rem; }
  header { padding: 1.6rem 0 1rem; }
  h1 { margin: 0; font-size: 1.5rem; }
  h1 span { color: #58a6ff; }
  p.sub { color: #8b949e; margin: 0.4rem 0 0; font-size: 0.95rem; }
  section { margin-top: 1.6rem; }
  h2 { font-size: 1.05rem; margin: 0 0 0.7rem; color: #c9d1d9; }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 1rem;
  }
  input[type=text], input[type=url], select {
    width: 100%; padding: 0.55rem 0.7rem; background: #0d1117; color: #e6edf3;
    border: 1px solid #30363d; border-radius: 8px; font-size: 0.95rem;
  }
  label { display: block; font-size: 0.8rem; color: #8b949e; margin: 0.9rem 0 0.3rem; }
  label:first-child { margin-top: 0; }
  button {
    background: #238636; color: #fff; border: 0; border-radius: 8px; padding: 0.6rem 1.1rem;
    font-size: 0.95rem; font-weight: 600; cursor: pointer; margin-top: 1rem; width: 100%;
  }
  button:hover { background: #2ea043; }
  button:disabled { background: #30363d; color: #8b949e; cursor: not-allowed; }
  button.ghost { background: #21262d; border: 1px solid #30363d; width: auto; margin: 0; padding: 0.4rem 0.8rem; font-weight: 400; }
  .row { display: flex; gap: 0.7rem; }
  .row > * { flex: 1; }
  .hint { font-size: 0.78rem; color: #8b949e; margin-top: 0.3rem; }
  .badge { display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600; }
  .badge.run { background: #1f6feb33; color: #58a6ff; }
  .badge.done { background: #23863633; color: #3fb950; }
  .badge.err { background: #da363333; color: #f85149; }
  .badge.idle { background: #30363d; color: #8b949e; }
  #results { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 0.7rem; }
  .hit {
    background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 0.7rem;
    cursor: pointer; display: flex; gap: 0.6rem; align-items: center;
  }
  .hit:hover { border-color: #58a6ff; }
  .hit img { width: 40px; height: 40px; border-radius: 8px; }
  .hit .t { font-size: 0.85rem; font-weight: 600; }
  .hit .d { font-size: 0.7rem; color: #8b949e; }
  .imp { display: flex; justify-content: space-between; align-items: center; padding: 0.5rem 0; border-bottom: 1px solid #21262d; font-size: 0.9rem; }
  .imp:last-child { border-bottom: 0; }
  pre { background: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 0.8rem; font-size: 0.75rem; overflow-x: auto; max-height: 300px; overflow-y: auto; }
  .ok { color: #3fb950; } .ko { color: #f85149; }
  #msg { margin-top: 0.8rem; font-size: 0.85rem; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Modpack <span>Importer</span></h1>
    <p class="sub">Modpacks, mods, plugins et maps de Modrinth, CurseForge et SpigotMC &rarr; pr&eacute;ts dans Crafty.</p>
  </header>

  <section>
    <div class="card">
      <h2>1. Rechercher un modpack Modrinth</h2>
      <div class="row">
        <input type="text" id="q" placeholder="ex : cobblemon, better mcs, create...">
        <button class="ghost" onclick="search()" style="margin:0">Chercher</button>
      </div>
      <div id="results" style="margin-top:0.8rem"></div>
    </div>
  </section>

  <section>
    <div class="card">
      <h2>2. Lancer l'import</h2>
      <label>Modpack (lien Modrinth ou CurseForge, ou slug Modrinth)</label>
      <input type="text" id="source" placeholder="https://modrinth.com/modpack/cobblemon-fabric">
      <div class="row">
        <div>
          <label>Version Java</label>
          <select id="java">
            <option value="java21">Java 21 &mdash; MC 1.20.5+</option>
            <option value="java17">Java 17 &mdash; MC 1.17 &agrave; 1.20.4</option>
          </select>
        </div>
        <div>
          <label>Loader (Modrinth, optionnel)</label>
          <select id="loader">
            <option value="">Auto</option>
            <option value="forge">Forge</option>
            <option value="fabric">Fabric</option>
            <option value="quilt">Quilt</option>
          </select>
        </div>
      </div>
      <label>Mods / plugins Modrinth en plus (slugs ou liens, s&eacute;par&eacute;s par des virgules)</label>
      <input type="text" id="mods" placeholder="ex : fabric-api, cloth-config">
      <label>Plugins SpigotMC (IDs de ressource, s&eacute;par&eacute;s par des virgules)</label>
      <input type="text" id="spiget" placeholder="ex : 81835, 32420">
      <label>Map (URL directe d'un zip de world, optionnel)</label>
      <input type="url" id="world" placeholder="https://.../map.zip">
      <button id="go" onclick="doImport()">Importer dans Crafty</button>
      <div id="msg"></div>
    </div>
  </section>

  <section>
    <div class="card">
      <h2>Statut <span id="badge" class="badge idle">inactif</span></h2>
      <div id="impList"></div>
      <details style="margin-top:0.8rem">
        <summary style="cursor:pointer;color:#8b949e;font-size:0.85rem">Voir le journal du dernier import</summary>
        <pre id="log">Pas d'import en cours.</pre>
      </details>
    </div>
  </section>

  <p class="hint">Une fois l'import termin&eacute; : ouvre Crafty Controller &rarr; Assistant d'import &rarr; le dossier du modpack y appara&icirc;t d&eacute;j&agrave;. Si besoin, red&eacute;marre l'app Crafty pour rafra&icirc;chir la liste.</p>
</div>

<script>
let lastName = null;

const $ = (id) => document.getElementById(id);

function fmt(n) {
  return n >= 1e6 ? (n/1e6).toFixed(1)+'M' : n >= 1e3 ? (n/1e3).toFixed(1)+'k' : n;
}

async function search() {
  const q = $('q').value.trim();
  const box = $('results');
  if (!q) return;
  box.innerHTML = '<p class="hint">Recherche...</p>';
  try {
    const r = await fetch('/api/search?q=' + encodeURIComponent(q));
    const d = await r.json();
    if (!d.hits || !d.hits.length) { box.innerHTML = '<p class="hint">Aucun modpack trouv&eacute;.</p>'; return; }
    box.innerHTML = d.hits.map(h => `
      <div class="hit" onclick="pick('${h.slug}')" title="${h.description}">
        ${h.icon_url ? `<img src="${h.icon_url}" alt="">` : ''}
        <div><div class="t">${h.title}</div><div class="d">${fmt(h.downloads)} t&eacute;l&eacute;chargements</div></div>
      </div>`).join('');
  } catch (e) {
    box.innerHTML = '<p class="hint">Erreur de recherche : ' + e + '</p>';
  }
}

function pick(slug) {
  $('source').value = slug;
  window.scrollTo({ top: document.body.scrollHeight * 0.45, behavior: 'smooth' });
}

async function doImport() {
  const body = {
    source: $('source').value.trim(),
    java: $('java').value,
    loader: $('loader').value,
    mods: $('mods').value,
    spiget: $('spiget').value,
    world: $('world').value.trim(),
  };
  $('go').disabled = true;
  $('msg').className = '';
  $('msg').textContent = 'Envoi...';
  try {
    const r = await fetch('/api/import', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (d.ok) {
      lastName = d.name;
      $('msg').className = 'ok';
      $('msg').textContent = 'Import lanc&eacute; : ' + d.name + '. Surveille le statut ci-dessous.';
    } else {
      $('msg').className = 'ko';
      $('msg').textContent = d.error;
    }
  } catch (e) {
    $('msg').className = 'ko';
    $('msg').textContent = 'Erreur : ' + e;
  }
  $('go').disabled = false;
}

async function refresh() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const s = d.status;
    const b = $('badge');
    if (s.state === 'running') { b.className = 'badge run'; b.textContent = 'import de ' + s.name; }
    else if (s.state === 'done') { b.className = 'badge done'; b.textContent = s.name + ' pr&ecirc;t'; lastName = s.name; }
    else if (s.state === 'error') { b.className = 'badge err'; b.textContent = s.name + ' en &eacute;chec'; lastName = s.name; }
    else { b.className = 'badge idle'; b.textContent = 'inactif'; }
    $('impList').innerHTML = d.imports.map(n =>
      `<div class="imp"><span>${n}</span><button class="ghost" onclick="showLog('${n}')">Journal</button></div>`
    ).join('') || '<p class="hint">Aucun modpack import&eacute; pour le moment.</p>';
    if (s.state === 'running' && s.name) lastName = s.name;
    if (lastName && $('log').dataset.live !== '0') showLog(lastName, true);
  } catch (e) { /* ignore */ }
}

async function showLog(name, quiet) {
  $('log').dataset.live = '1';
  try {
    const r = await fetch('/api/log/' + encodeURIComponent(name));
    $('log').textContent = await r.text();
  } catch (e) { if (!quiet) $('log').textContent = 'Erreur: ' + e; }
}

$('q').addEventListener('keydown', e => { if (e.key === 'Enter') search(); });
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, content_type="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.reply(200, PAGE, "text/html")
            return

        if parsed.path == "/api/search":
            query = (urllib.parse.parse_qs(parsed.query).get("q") or [""])[0].strip()
            if not query:
                self.reply(400, json.dumps({"error": "Requête vide"}))
                return
            try:
                self.reply(200, json.dumps({"hits": modrinth_search(query)}))
            except Exception as exc:
                self.reply(502, json.dumps({"error": f"Modrinth injoignable : {exc}"}))
            return

        if parsed.path == "/api/status":
            self.reply(200, json.dumps(status_payload()))
            return

        m = re.fullmatch(r"/api/log/([A-Za-z0-9._-]+)", parsed.path)
        if m:
            name = m.group(1)
            path = os.path.join(LOGS_DIR, name + ".log")
            if re.fullmatch(r"[A-Za-z0-9._-]+", name) and os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    self.reply(200, fh.read()[-20000:], "text/plain")
            else:
                self.reply(404, "Journal introuvable.", "text/plain")
            return

        self.reply(404, json.dumps({"error": "Introuvable"}))

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/import":
            self.reply(404, json.dumps({"error": "Introuvable"}))
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            self.reply(400, json.dumps({"error": "JSON invalide"}))
            return

        try:
            source = (data.get("source") or "").strip()
            if not source:
                raise ValueError("Colle un lien de modpack ou un slug Modrinth.")
            java = data.get("java") or "java21"
            if java not in ALLOWED_JAVA:
                raise ValueError("Version Java invalide.")
            mode, name, env = parse_source(source)
            if mode == "modrinth" and not modrinth_project_exists(env["MODRINTH_MODPACK"]):
                raise ValueError(
                    "Modpack Modrinth introuvable. Vérifie le lien/slug "
                    "(ou colle une page CurseForge complète)."
                )

            loader = (data.get("loader") or "").strip()
            if loader and loader in ALLOWED_LOADERS:
                env["MODRINTH_LOADER"] = loader

            mc_version = (data.get("mc_version") or "").strip()
            if mc_version and re.fullmatch(r"[\d.]+", mc_version):
                env["VERSION"] = mc_version

            mods = modrinth_slug_list(data.get("mods") or "")
            if mods:
                env["MODRINTH_PROJECTS"] = ",".join(mods)

            spiget = spiget_id_list(data.get("spiget") or "")
            if spiget:
                env["SPIGET_RESOURCES"] = ",".join(spiget)

            world = (data.get("world") or "").strip()
            if world and world.startswith(("http://", "https://")):
                env["WORLD"] = world

            write_request(java, name, env)
            self.reply(200, json.dumps({"ok": True, "name": name}))
        except ValueError as exc:
            self.reply(400, json.dumps({"error": str(exc)}))
        except Exception as exc:
            self.reply(500, json.dumps({"error": f"Erreur serveur : {exc}"}))


def main():
    os.makedirs(QUEUE_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    print("Modpack Importer web listening on :8080", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
