"""
edge_mcp_server.py — MCP server pour piloter Edge via CDP/Playwright
Compatible avec tout LLM supportant le protocole MCP (Claude, Qwen, Mistral, Ollama...)

Prérequis :
    pip install mcp playwright --break-system-packages
    # Edge lancé avec : msedge.exe --remote-debugging-port=9222 --remote-allow-origins=*
    # Tunnel SSH si Edge est sur Windows : ssh -L 9222:localhost:9222 user@windows-host
"""

from mcp.server.fastmcp import FastMCP
from playwright.sync_api import sync_playwright, Browser, Page, Error as PWError
import base64
import json
import threading
import re

# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "edge-browser",
    instructions=(
        "Pilote un navigateur Edge via CDP. "
        "Commence toujours par get_page_summary() pour comprendre la page courante. "
        "Utilise list_interactive_elements() pour savoir quoi cliquer. "
        "Préfère les outils sémantiques (click_text, find_element) aux sélecteurs CSS bruts. "
        "Chaque outil retourne {status, data, current_url} pour que tu restes orienté."
    ),
)

_playwright = None
_browser: Browser | None = None
_lock = threading.Lock()
_CDP_URL = "http://localhost:9222"


# ---------------------------------------------------------------------------
# Gestion de la connexion
# ---------------------------------------------------------------------------

def _get_browser() -> Browser:
    global _playwright, _browser
    with _lock:
        if _browser is None or not _browser.is_connected():
            if _playwright:
                try:
                    _playwright.stop()
                except Exception:
                    pass
            _playwright = sync_playwright().start()
            _browser = _playwright.chromium.connect_over_cdp(_CDP_URL)
    return _browser


def _get_page(tab_index: int = 0) -> Page:
    browser = _get_browser()
    ctx = browser.contexts[0]
    pages = ctx.pages
    if not pages:
        raise RuntimeError("Aucun onglet ouvert dans Edge.")
    idx = min(tab_index, len(pages) - 1)
    return pages[idx]


def _ok(data, page: Page | None = None) -> str:
    return json.dumps({
        "status": "ok",
        "data": data,
        "current_url": page.url if page else None,
    }, ensure_ascii=False)


def _err(msg: str, page: Page | None = None) -> str:
    return json.dumps({
        "status": "error",
        "error": msg,
        "current_url": page.url if page else None,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Outils — Navigation
# ---------------------------------------------------------------------------

@mcp.tool()
def navigate(url: str, tab_index: int = 0) -> str:
    """
    Navigue vers une URL.
    Args:
        url: URL complète (avec https://)
        tab_index: index de l'onglet (0 = premier onglet actif)
    Returns JSON: {status, data: {title, url}, current_url}
    """
    try:
        page = _get_page(tab_index)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWError:
        pass  # networkidle peut timeout sur des apps dynamiques
    except Exception as e:
        return _err(str(e))
    return _ok({"title": page.title(), "url": page.url}, page)


@mcp.tool()
def go_back(tab_index: int = 0) -> str:
    """Retourne à la page précédente (bouton Retour)."""
    try:
        page = _get_page(tab_index)
        page.go_back(wait_until="domcontentloaded", timeout=10000)
        return _ok({"title": page.title()}, page)
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def reload(tab_index: int = 0) -> str:
    """Recharge la page courante."""
    try:
        page = _get_page(tab_index)
        page.reload(wait_until="domcontentloaded", timeout=15000)
        return _ok({"title": page.title()}, page)
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Outils — Observation
# ---------------------------------------------------------------------------

@mcp.tool()
def get_page_summary(tab_index: int = 0) -> str:
    """
    Retourne un résumé lisible de la page : titre, URL, texte principal (Markdown).
    Utilise cet outil en premier pour comprendre où tu es.
    """
    try:
        page = _get_page(tab_index)
        title = page.title()
        url = page.url

        # Extraction du contenu texte structuré via JS
        summary = page.evaluate("""() => {
            const remove = ['script','style','nav','footer','head','noscript','svg','iframe'];
            const clone = document.body.cloneNode(true);
            remove.forEach(tag => clone.querySelectorAll(tag).forEach(el => el.remove()));

            const lines = [];
            clone.querySelectorAll('h1,h2,h3,h4,p,li,label,th,td,button,a,input,select,textarea')
                .forEach(el => {
                    const text = el.innerText?.trim();
                    if (!text || text.length < 2) return;
                    const tag = el.tagName.toLowerCase();
                    if (tag === 'h1') lines.push('# ' + text);
                    else if (tag === 'h2') lines.push('## ' + text);
                    else if (tag === 'h3') lines.push('### ' + text);
                    else if (tag === 'li') lines.push('- ' + text);
                    else lines.push(text);
                });

            // Dédoublonner les lignes consécutives identiques
            return [...new Set(lines)].slice(0, 200).join('\\n');
        }""")

        return _ok({
            "title": title,
            "url": url,
            "content": summary[:8000],  # ~8k chars max pour le contexte du modèle
        }, page)
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def list_interactive_elements(tab_index: int = 0) -> str:
    """
    Liste tous les éléments cliquables/remplissables visibles.
    Retourne: boutons, liens, champs, selects — avec leur texte et sélecteur CSS.
    Utilise cet outil avant click ou fill pour savoir quoi cibler.
    """
    try:
        page = _get_page(tab_index)
        elements = page.evaluate("""() => {
            const results = [];
            const seen = new Set();

            const toCSS = el => {
                if (el.id) return '#' + el.id;
                if (el.name) return `[name="${el.name}"]`;
                const cls = [...el.classList].filter(c => /^[a-zA-Z]/.test(c)).slice(0, 2).join('.');
                return cls ? el.tagName.toLowerCase() + '.' + cls : el.tagName.toLowerCase();
            };

            document.querySelectorAll('button,a,input,select,textarea,[role=button],[role=link],[role=tab],[role=menuitem],[role=option],[onclick]')
                .forEach(el => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 && rect.height === 0) return;
                    const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().slice(0, 80);
                    if (!text) return;
                    const key = el.tagName + '|' + text;
                    if (seen.has(key)) return;
                    seen.add(key);
                    results.push({
                        type: el.tagName.toLowerCase(),
                        text,
                        selector: toCSS(el),
                        role: el.getAttribute('role') || null,
                    });
                });
            return results.slice(0, 60);
        }""")

        return _ok({"count": len(elements), "elements": elements}, page)
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def list_tabs() -> str:
    """
    Liste tous les onglets ouverts dans Edge.
    Returns: [{index, title, url}]
    """
    try:
        browser = _get_browser()
        pages = browser.contexts[0].pages
        tabs = [{"index": i, "title": p.title(), "url": p.url} for i, p in enumerate(pages)]
        return _ok({"count": len(tabs), "tabs": tabs})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def screenshot(tab_index: int = 0, full_page: bool = False) -> str:
    """
    Prend une capture d'écran de la page.
    Args:
        full_page: True pour capturer toute la page (pas seulement le viewport)
    Returns: image PNG encodée en base64
    """
    try:
        page = _get_page(tab_index)
        data = page.screenshot(type="png", full_page=full_page)
        return _ok({
            "image_base64": base64.b64encode(data).decode(),
            "format": "png",
            "note": "Décode le base64 pour afficher l'image",
        }, page)
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def get_element_text(selector: str, tab_index: int = 0) -> str:
    """
    Lit le texte d'un élément par sélecteur CSS.
    Args:
        selector: sélecteur CSS (ex: '#result', '.total', 'table')
    """
    try:
        page = _get_page(tab_index)
        el = page.query_selector(selector)
        if not el:
            return _err(f"Élément introuvable : {selector}", page)
        return _ok({"text": el.inner_text().strip()}, page)
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def find_element(text: str, tab_index: int = 0) -> str:
    """
    Cherche un élément par son texte visible (recherche partielle, insensible à la casse).
    Plus robuste que les sélecteurs CSS quand la page change souvent.
    Args:
        text: texte à chercher (ex: 'Connexion', 'Submit', 'Chargement')
    Returns: liste des éléments trouvés avec leur sélecteur
    """
    try:
        page = _get_page(tab_index)
        results = page.evaluate(f"""() => {{
            const needle = {json.dumps(text.lower())};
            const found = [];
            document.querySelectorAll('*').forEach(el => {{
                const t = (el.innerText || el.value || '').trim().toLowerCase();
                if (t.includes(needle) && el.children.length === 0) {{
                    const id = el.id ? '#' + el.id : null;
                    found.push({{
                        tag: el.tagName.toLowerCase(),
                        text: (el.innerText || el.value || '').trim().slice(0, 80),
                        selector: id || el.tagName.toLowerCase(),
                    }});
                }}
            }});
            return found.slice(0, 10);
        }}""")
        return _ok({"matches": results, "count": len(results)}, page)
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Outils — Interaction
# ---------------------------------------------------------------------------

@mcp.tool()
def click(selector: str, tab_index: int = 0) -> str:
    """
    Clique sur un élément par sélecteur CSS.
    Si tu ne connais pas le sélecteur, utilise click_text() à la place.
    Args:
        selector: sélecteur CSS (ex: '#submit-btn', '.nav-link', 'button[type=submit]')
    """
    try:
        page = _get_page(tab_index)
        page.click(selector, timeout=8000)
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWError:
        pass
    except Exception as e:
        return _err(str(e))
    return _ok({"action": "clicked", "selector": selector}, page)


@mcp.tool()
def click_text(text: str, exact: bool = False, tab_index: int = 0) -> str:
    """
    Clique sur un élément en cherchant son texte visible.
    Meilleur choix quand tu ne connais pas le sélecteur CSS exact.
    Args:
        text: texte du bouton/lien (ex: 'Se connecter', 'OK', 'Valider')
        exact: True pour correspondance exacte, False pour correspondance partielle
    """
    try:
        page = _get_page(tab_index)
        locator = page.get_by_text(text, exact=exact)
        locator.first.click(timeout=8000)
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWError:
        pass
    except Exception as e:
        return _err(str(e))
    return _ok({"action": "clicked_text", "text": text}, page)


@mcp.tool()
def click_role(role: str, name: str = "", tab_index: int = 0) -> str:
    """
    Clique sur un élément par son rôle ARIA et son nom accessible.
    Très robuste sur les apps modernes avec des composants dynamiques.
    Args:
        role: rôle ARIA (button, link, tab, checkbox, combobox, menuitem, option...)
        name: texte du nom accessible (partiel suffit)
    """
    try:
        page = _get_page(tab_index)
        kwargs = {"name": re.compile(name, re.IGNORECASE)} if name else {}
        page.get_by_role(role, **kwargs).first.click(timeout=8000)
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWError:
        pass
    except Exception as e:
        return _err(str(e))
    return _ok({"action": "clicked_role", "role": role, "name": name}, page)


@mcp.tool()
def fill(selector: str, value: str, tab_index: int = 0) -> str:
    """
    Remplit un champ texte par sélecteur CSS.
    Args:
        selector: sélecteur CSS du champ (ex: '#username', 'input[name=search]')
        value: valeur à saisir
    """
    try:
        page = _get_page(tab_index)
        page.fill(selector, value, timeout=8000)
        return _ok({"action": "filled", "selector": selector, "value": value}, page)
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def fill_label(label_text: str, value: str, tab_index: int = 0) -> str:
    """
    Remplit un champ en le trouvant par son label visible.
    Préférable à fill() quand tu vois le libellé du champ sur la page.
    Args:
        label_text: texte du label (ex: 'Email', 'Mot de passe', 'Période')
        value: valeur à saisir
    """
    try:
        page = _get_page(tab_index)
        page.get_by_label(label_text).fill(value, timeout=8000)
        return _ok({"action": "filled_label", "label": label_text, "value": value}, page)
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def select_option(selector: str, value: str = "", label: str = "", tab_index: int = 0) -> str:
    """
    Sélectionne une option dans un <select>.
    Args:
        selector: sélecteur CSS du select
        value: valeur de l'option (attribut value=)
        label: texte visible de l'option (utilisé si value vide)
    """
    try:
        page = _get_page(tab_index)
        if value:
            page.select_option(selector, value=value, timeout=8000)
        elif label:
            page.select_option(selector, label=label, timeout=8000)
        else:
            return _err("Fournis value ou label")
        return _ok({"action": "selected", "selector": selector}, page)
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def press_key(key: str, selector: str = "", tab_index: int = 0) -> str:
    """
    Appuie sur une touche clavier (sur un élément ou globalement).
    Args:
        key: touche (ex: 'Enter', 'Tab', 'Escape', 'ArrowDown', 'Control+a')
        selector: sélecteur CSS de l'élément ciblé (optionnel, sinon touche globale)
    """
    try:
        page = _get_page(tab_index)
        if selector:
            page.press(selector, key, timeout=8000)
        else:
            page.keyboard.press(key)
        return _ok({"action": "key_pressed", "key": key}, page)
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def scroll(direction: str = "down", amount: int = 500, tab_index: int = 0) -> str:
    """
    Fait défiler la page.
    Args:
        direction: 'down', 'up', 'bottom', 'top'
        amount: pixels à défiler (ignoré pour 'bottom'/'top')
    """
    try:
        page = _get_page(tab_index)
        scripts = {
            "down": f"window.scrollBy(0, {amount})",
            "up": f"window.scrollBy(0, -{amount})",
            "bottom": "window.scrollTo(0, document.body.scrollHeight)",
            "top": "window.scrollTo(0, 0)",
        }
        page.evaluate(scripts.get(direction, scripts["down"]))
        return _ok({"action": "scrolled", "direction": direction}, page)
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Outils — Attente et synchronisation
# ---------------------------------------------------------------------------

@mcp.tool()
def wait_for_text(text: str, timeout: int = 15000, tab_index: int = 0) -> str:
    """
    Attend qu'un texte apparaisse sur la page (utile après une action asynchrone).
    Args:
        text: texte à attendre
        timeout: délai max en millisecondes (défaut 15s)
    """
    try:
        page = _get_page(tab_index)
        page.get_by_text(text).first.wait_for(state="visible", timeout=timeout)
        return _ok({"found": True, "text": text}, page)
    except Exception as e:
        return _err(f"Texte '{text}' non trouvé : {e}")


@mcp.tool()
def wait_for_selector(selector: str, timeout: int = 10000, tab_index: int = 0) -> str:
    """
    Attend qu'un sélecteur CSS apparaisse.
    Args:
        selector: sélecteur CSS
        timeout: délai max en millisecondes (défaut 10s)
    """
    try:
        page = _get_page(tab_index)
        page.wait_for_selector(selector, timeout=timeout)
        return _ok({"found": True, "selector": selector}, page)
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def wait_for_navigation(timeout: int = 15000, tab_index: int = 0) -> str:
    """
    Attend que la navigation soit terminée (après un clic qui déclenche un chargement).
    Args:
        timeout: délai max en millisecondes
    """
    try:
        page = _get_page(tab_index)
        page.wait_for_load_state("networkidle", timeout=timeout)
        return _ok({"loaded": True, "title": page.title()}, page)
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Outils — JavaScript avancé
# ---------------------------------------------------------------------------

@mcp.tool()
def run_js(script: str, tab_index: int = 0) -> str:
    """
    Exécute du JavaScript dans la page et retourne le résultat.
    Utile pour des interactions non couvertes par les autres outils.
    Args:
        script: code JS (ex: 'document.title', 'document.querySelector("#id").value')
    Returns: résultat sérialisé en JSON
    """
    try:
        page = _get_page(tab_index)
        result = page.evaluate(script)
        return _ok({"result": result}, page)
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def get_table(selector: str = "table", tab_index: int = 0) -> str:
    """
    Extrait le contenu d'un tableau HTML sous forme de liste de dicts.
    Args:
        selector: sélecteur CSS du tableau (défaut: 'table' = premier tableau)
    Returns: [{colonne: valeur, ...}, ...]
    """
    try:
        page = _get_page(tab_index)
        data = page.evaluate(f"""() => {{
            const table = document.querySelector({json.dumps(selector)});
            if (!table) return null;
            const headers = [...table.querySelectorAll('th')].map(th => th.innerText.trim());
            const rows = [...table.querySelectorAll('tbody tr')].map(tr => {{
                const cells = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
                return headers.length ? Object.fromEntries(headers.map((h, i) => [h, cells[i] ?? ''])) : cells;
            }});
            return rows;
        }}""")
        if data is None:
            return _err(f"Tableau '{selector}' introuvable", page)
        return _ok({"rows": data, "count": len(data)}, page)
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Entrée
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
