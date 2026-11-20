#!/usr/bin/env python3
"""Fradgify DLNA/UPnP media server (multi-source).

Serves one or more remote mp3 libraries (Apache autoindex trees) as a
single UPnP MediaServer. The root container lists the sources by name
(e.g. "Fradgify", "Induce"); each source keeps its own directory tree.
Media streams and cover art are referenced directly at the source
website (Range requests supported), so nothing is proxied here.

Sources are persisted in /data/sources.json. New ones can be added or
removed through the web form at /admin (port FRADGIFY_PORT).
"""

import concurrent.futures
import gzip
import html
import json
import os
import random
import re
import shutil
import signal
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"

DATA_DIR = os.environ.get("FRADGIFY_DATA_DIR", "/data")
INDEX_DIR = os.environ.get("FRADGIFY_INDEX_DIR", "/index")
PORT = int(os.environ.get("FRADGIFY_PORT", "9092"))
DLNA_NAME = os.environ.get("FRADGIFY_DLNA_NAME", "web-dlna").strip() or "web-dlna"
DLNA_IP = os.environ.get("FRADGIFY_DLNA_IP", "").strip()
DLNA_NOTIFY = max(5, int(os.environ.get("FRADGIFY_NOTIFY", "30")))
WORKERS = max(1, int(os.environ.get("FRADGIFY_WORKERS", "10")))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")
WEB_DIR = os.environ.get("FRADGIFY_WEB_DIR",
                         os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))

AUDIO_EXT = {
    ".mp3": ("audio/mpeg", "MP3"),
    ".flac": ("audio/flac", "FLAC"),
    ".ogg": ("audio/ogg", "OGG"),
    ".opus": ("audio/ogg", "OGG"),
    ".m4a": ("audio/mp4", "AAC"),
    ".aac": ("audio/aac", "AAC"),
    ".wav": ("audio/wav", ""),
}
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

DEFAULT_SOURCES = [
    {"id": "fradgify", "name": "Fradgify",
     "base_url": "https://www.fradgify.kozow.com/media/music/complete/",
     "icon": "https://www.fradgify.kozow.com/static/icons/favicon.ico"},
    {"id": "induce", "name": "Induce",
     "base_url": "https://www.induceonline.com/live/",
     "icon": "https://induceonline.com/favicon.ico"},
]

LANGS = ("de", "fr", "en", "es", "zh", "hi")
LANG_NAMES = {"de": "Deutsch", "fr": "Français", "en": "English",
              "es": "Español", "zh": "中文", "hi": "हिन्दी"}

T = {
    "de": {
        "lang": "Sprache", "th_source": "Quelle", "th_url": "URL",
        "th_dirs": "Ordner", "th_files": "Dateien", "th_mp3": "MP3", "th_genre": "Genre", "no_genre": "(ohne Ordner)", "download": "Download", "dl_jobs": "Downloads", "dl_folder": "Ordner", "dl_status": "Status", "dl_files": "Dateien", "dl_size": "Größe", "dl_dest": "Ziel", "dl_cancel": "Abbrechen", "dl_queued": "in Warteschlange", "dl_running": "läuft", "dl_done": "fertig", "dl_cancelled": "abgebrochen", "dl_empty": "Keine Downloads.", "dl_link": "Link", "dl_track": "Track", "dl_name": "Name", "dl_date": "Datum", "dl_history": "Verlauf", "dl_skipped": "vorhanden", "dl_free": "Frei: %s",
        "th_images": "Bilder", "th_status": "Status", "sum": "Summe",
        "updated": "Aktualisiert: %s", "never": "nie",
        "manage": "Admin", "rescan": "Alles neu suchen",
        "json": "JSON summary", "device": "device.xml",
        "h_sources": "Quellen (DLNA-Bäume)", "new_source": "Neue Quelle",
        "name": "Name", "url": "URL des Apache-Index:", "name_ph": "z. B. Induce",
        "url_ph": "https://beispiel.de/ordner/", "add": "Hinzufügen &amp; suchen",
        "confirm_add": "Diese URL wird jetzt durchsucht.",
        "back": "Zurück zum Dashboard", "remove": "Entfernen",
        "confirm": "%s wirklich entfernen?", "st_building": "wird aktualisiert…",
        "st_ok": "ok", "st_error": "Fehler", "msg_added": "Quelle hinzugefügt – Suche läuft.",
        "msg_removed": "Quelle entfernt.", "msg_notfound": "Quelle nicht gefunden.",
        "err_scheme": "URL muss mit http:// oder https:// beginnen",
        "err_exists": "Quelle existiert schon: {name}",
        "err_unreachable": "Seite nicht erreichbar: {err}",
        "err_noindex": "Kein Index mit Dateien/Ordnern gefunden unter {url}",
        "err_name": "Name ist ein Pflichtfeld.",
    },
    "fr": {
        "lang": "Langue", "th_source": "Source", "th_url": "URL",
        "th_dirs": "Dossiers", "th_files": "Fichiers", "th_mp3": "MP3", "th_genre": "Genre", "no_genre": "(sans dossier)", "download": "Télécharger", "dl_jobs": "Téléchargements", "dl_folder": "Dossier", "dl_status": "Statut", "dl_files": "Fichiers", "dl_size": "Taille", "dl_dest": "Destination", "dl_cancel": "Annuler", "dl_queued": "en attente", "dl_running": "en cours", "dl_done": "terminé", "dl_cancelled": "annulé", "dl_empty": "Aucun téléchargement.", "dl_link": "Lien", "dl_track": "Piste", "dl_name": "Nom", "dl_date": "Date", "dl_history": "Historique", "dl_skipped": "déjà présent", "dl_free": "Libre : %s",
        "th_images": "Images", "th_status": "Statut", "sum": "Total",
        "updated": "Mis à jour : %s", "never": "jamais",
        "manage": "Admin", "rescan": "Tout rescanner",
        "json": "JSON summary", "device": "device.xml",
        "h_sources": "Sources (arborescences DLNA)", "new_source": "Nouvelle source",
        "name": "Nom", "url": "URL de l'index Apache :", "name_ph": "p. ex. Induce",
        "url_ph": "https://exemple.fr/dossier/", "add": "Ajouter &amp; chercher",
        "confirm_add": "Cette URL va être analysée maintenant.",
        "back": "Retour au tableau de bord", "remove": "Supprimer",
        "confirm": "Supprimer vraiment « %s » ?", "st_building": "mise à jour…",
        "st_ok": "ok", "st_error": "erreur", "msg_added": "Source ajoutée – analyse en cours.",
        "msg_removed": "Source supprimée.", "msg_notfound": "Source introuvable.",
        "err_scheme": "L'URL doit commencer par http:// ou https://",
        "err_exists": "La source existe déjà : {name}",
        "err_unreachable": "Site injoignable : {err}",
        "err_noindex": "Aucun index de fichiers/dossiers trouvé sous {url}",
        "err_name": "Le nom est obligatoire.",
    },
    "en": {
        "lang": "Language", "th_source": "Source", "th_url": "URL",
        "th_dirs": "Folders", "th_files": "Files", "th_mp3": "MP3", "th_genre": "Genre", "no_genre": "(no folder)", "download": "Download", "dl_jobs": "Downloads", "dl_folder": "Folder", "dl_status": "Status", "dl_files": "Files", "dl_size": "Size", "dl_dest": "Destination", "dl_cancel": "Cancel", "dl_queued": "queued", "dl_running": "running", "dl_done": "done", "dl_cancelled": "cancelled", "dl_empty": "No downloads.", "dl_link": "Link", "dl_track": "Track", "dl_name": "Name", "dl_date": "Date", "dl_history": "History", "dl_skipped": "already present", "dl_free": "Free: %s",
        "th_images": "Images", "th_status": "Status", "sum": "Total",
        "updated": "Updated: %s", "never": "never",
        "manage": "Admin", "rescan": "Rescan all",
        "json": "JSON summary", "device": "device.xml",
        "h_sources": "Sources (DLNA trees)", "new_source": "New source",
        "name": "Name", "url": "Apache index URL:", "name_ph": "e.g. Induce",
        "url_ph": "https://example.com/folder/", "add": "Add &amp; scan",
        "confirm_add": "This URL will be scanned now.",
        "back": "Back to dashboard", "remove": "Remove",
        "confirm": "Really remove %s?", "st_building": "updating…",
        "st_ok": "ok", "st_error": "error", "msg_added": "Source added – scan running.",
        "msg_removed": "Source removed.", "msg_notfound": "Source not found.",
        "err_scheme": "URL must start with http:// or https://",
        "err_exists": "Source already exists: {name}",
        "err_unreachable": "Site unreachable: {err}",
        "err_noindex": "No index with files/folders found under {url}",
        "err_name": "Name is required.",
    },
    "es": {
        "lang": "Idioma", "th_source": "Fuente", "th_url": "URL",
        "th_dirs": "Carpetas", "th_files": "Archivos", "th_mp3": "MP3", "th_genre": "Género", "no_genre": "(sin carpeta)", "download": "Descargar", "dl_jobs": "Descargas", "dl_folder": "Carpeta", "dl_status": "Estado", "dl_files": "Archivos", "dl_size": "Tamaño", "dl_dest": "Destino", "dl_cancel": "Cancelar", "dl_queued": "en cola", "dl_running": "en curso", "dl_done": "hecho", "dl_cancelled": "cancelado", "dl_empty": "No hay descargas.", "dl_link": "Enlace", "dl_track": "Pista", "dl_name": "Nombre", "dl_date": "Fecha", "dl_history": "Historial", "dl_skipped": "ya existe", "dl_free": "Libre: %s",
        "th_images": "Imágenes", "th_status": "Estado", "sum": "Total",
        "updated": "Actualizado: %s", "never": "nunca",
        "manage": "Admin", "rescan": "Reexplorar todo",
        "json": "JSON summary", "device": "device.xml",
        "h_sources": "Fuentes (árboles DLNA)", "new_source": "Nueva fuente",
        "name": "Nombre", "url": "URL del índice Apache:", "name_ph": "p. ej. Induce",
        "url_ph": "https://ejemplo.es/carpeta/", "add": "Añadir y buscar",
        "confirm_add": "Esta URL se analizará ahora.",
        "back": "Volver al panel", "remove": "Eliminar",
        "confirm": "¿Eliminar %s de verdad?", "st_building": "actualizando…",
        "st_ok": "ok", "st_error": "error", "msg_added": "Fuente añadida: búsqueda en curso.",
        "msg_removed": "Fuente eliminada.", "msg_notfound": "Fuente no encontrada.",
        "err_scheme": "La URL debe empezar por http:// o https://",
        "err_exists": "La fuente ya existe: {name}",
        "err_unreachable": "Sitio inaccesible: {err}",
        "err_noindex": "No se encontró un índice con archivos/carpetas en {url}",
        "err_name": "El nombre es obligatorio.",
    },
    "zh": {
        "lang": "语言", "th_source": "来源", "th_url": "URL",
        "th_dirs": "文件夹", "th_files": "文件", "th_mp3": "MP3", "th_genre": "类型", "no_genre": "（无文件夹）", "download": "下载", "dl_jobs": "下载", "dl_folder": "文件夹", "dl_status": "状态", "dl_files": "文件", "dl_size": "大小", "dl_dest": "目标", "dl_cancel": "取消", "dl_queued": "排队中", "dl_running": "进行中", "dl_done": "完成", "dl_cancelled": "已取消", "dl_empty": "没有下载。", "dl_link": "链接", "dl_track": "曲目", "dl_name": "名称", "dl_date": "日期", "dl_history": "历史", "dl_skipped": "已存在", "dl_free": "空闲: %s",
        "th_images": "图片", "th_status": "状态", "sum": "合计",
        "updated": "已更新：%s", "never": "从未",
        "manage": "Admin", "rescan": "全部重新扫描",
        "json": "JSON summary", "device": "device.xml",
        "h_sources": "源（DLNA 目录树）", "new_source": "新增源",
        "name": "名称", "url": "Apache 索引 URL：", "name_ph": "例如 Induce",
        "url_ph": "https://example.com/folder/", "add": "添加并扫描",
        "confirm_add": "将立即扫描此 URL。",
        "back": "返回仪表板", "remove": "删除",
        "confirm": "确实要删除 %s 吗？", "st_building": "正在更新…",
        "st_ok": "ok", "st_error": "错误", "msg_added": "已添加源 – 正在扫描。",
        "msg_removed": "已删除源。", "msg_notfound": "未找到源。",
        "err_scheme": "URL 必须以 http:// 或 https:// 开头",
        "err_exists": "源已存在：{name}",
        "err_unreachable": "无法访问站点：{err}",
        "err_noindex": "在 {url} 下未找到包含文件/文件夹的索引",
        "err_name": "名称是必填项。",
    },
    "hi": {
        "lang": "भाषा", "th_source": "स्रोत", "th_url": "URL",
        "th_dirs": "फ़ोल्डर", "th_files": "फ़ाइलें", "th_mp3": "MP3", "th_genre": "शैली", "no_genre": "(बिना फ़ोल्डर)", "download": "डाउनलोड", "dl_jobs": "डाउनलोड", "dl_folder": "फ़ोल्डर", "dl_status": "स्थिति", "dl_files": "फ़ाइलें", "dl_size": "आकार", "dl_dest": "गंतव्य", "dl_cancel": "रद्द करें", "dl_queued": "कतार में", "dl_running": "चल रहा है", "dl_done": "पूर्ण", "dl_cancelled": "रद्द", "dl_empty": "कोई डाउनलोड नहीं।", "dl_link": "लिंक", "dl_track": "ट्रैक", "dl_name": "नाम", "dl_date": "दिनांक", "dl_history": "इतिहास", "dl_skipped": "पहले से मौजूद", "dl_free": "खाली: %s",
        "th_images": "चित्र", "th_status": "स्थिति", "sum": "कुल",
        "updated": "अद्यतन: %s", "never": "कभी नहीं",
        "manage": "Admin", "rescan": "सभी फिर से स्कैन करें",
        "json": "JSON summary", "device": "device.xml",
        "h_sources": "स्रोत (DLNA ट्री)", "new_source": "नया स्रोत",
        "name": "नाम", "url": "Apache इंडेक्स URL:", "name_ph": "उदा. Induce",
        "url_ph": "https://example.com/folder/", "add": "जोड़ें और स्कैन करें",
        "confirm_add": "यह URL अभी स्कैन किया जाएगा।",
        "back": "डैशबोर्ड पर वापस जाएँ", "remove": "हटाएँ",
        "confirm": "क्या वाकई %s हटाएँ?", "st_building": "अद्यतन हो रहा है…",
        "st_ok": "ok", "st_error": "त्रुटि", "msg_added": "स्रोत जोड़ा गया – स्कैन चल रहा है।",
        "msg_removed": "स्रोत हटा दिया गया।", "msg_notfound": "स्रोत नहीं मिला।",
        "err_scheme": "URL http:// या https:// से शुरू होनी चाहिए",
        "err_exists": "स्रोत पहले से मौजूद है: {name}",
        "err_unreachable": "साइट उपलब्ध नहीं: {err}",
        "err_noindex": "{url} के अंतर्गत फ़ाइल/फ़ोल्डर इंडेक्स नहीं मिला",
        "err_name": "नाम आवश्यक है।",
    },
}


class SourceError(Exception):
    def __init__(self, key, **kw):
        super().__init__(key)
        self.key = key
        self.kw = kw


def request_lang(handler):
    cookie = handler.headers.get("Cookie") or ""
    for part in cookie.split(";"):
        if "=" in part:
            key, value = part.strip().split("=", 1)
            if key == "lang" and value in T:
                return value
    return "de"


SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_SERVER = "Linux/UPnP/1.0 web-dlna/" + VERSION
UPNP_DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaServer:1"
CD_SERVICE = "urn:schemas-upnp-org:service:ContentDirectory:1"
CM_SERVICE = "urn:schemas-upnp-org:service:ConnectionManager:1"
UPNP_TARGETS = [UPNP_DEVICE_TYPE, CD_SERVICE, CM_SERVICE]
DIDL_NS = (
    'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
    'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/"'
)

_lock = threading.Lock()
_state = {
    "sources": [],    # [{"id", "name", "base_url", "icon"}]
    "entries": {},    # "sid/rel" -> {"t": "d"|"f"|"i", "name": str, "ext": str}
    "children": {},   # "sid/rel" (dir) -> ["sid/rel", ...] sorted
    "updated": 0,
    "building": {},   # sid -> bool
    "errors": [],
}
_last_save = [0.0]
_dlna_server = None
DLNA_BASE = ""


def log(*args):
    print(*args, flush=True)


def xml_escape(value):
    return html.escape(str(value or ""), quote=False)


def fmt_int(n):
    return "{:,}".format(int(n)).replace(",", ".")


def fmt_bytes(n):
    n = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0
    return "0 B"


def m3u_q(value):
    return (value.replace("&", "%26").replace("#", "%23")
            .replace("+", "%2B").replace("=", "%3D").replace("?", "%3F"))


def status_badge(tr, sid, building, errors):
    if building:
        return "<span class='st st-build'>%s</span>" % xml_escape(tr["st_building"])
    cls = "st-err" if errors else "st-ok"
    text = tr.get("st_error", "error") if errors else tr["st_ok"]
    title = ("%d · %s" % (len(errors), tr["rescan"])) if errors else tr["rescan"]
    return ("<a class='st %s' href='#' title='%s' "
            "onclick=\"fetch('/api/refresh?sid=%s',{method:'POST'});"
            "setTimeout(function(){location.reload()},1500);return false\">%s</a>"
            % (cls, xml_escape(title), urllib.parse.quote(sid), xml_escape(text)))


def detect_ip():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((SSDP_ADDR, SSDP_PORT))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def device_uuid():
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "dlna.uuid")
    try:
        val = open(path, encoding="utf-8").read().strip()
        if val:
            return val
    except OSError:
        pass
    val = str(uuid.uuid4())
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(val)
    except OSError:
        pass
    return val


def base_url():
    return DLNA_BASE or "http://%s:%d" % (DLNA_IP or detect_ip(), PORT)


def srel(sid, rel):
    return "%s/%s" % (sid, rel)


def sid_of(key):
    return key.split("/", 1)[0]


def display_name(rel):
    name = rel.rsplit("/", 1)[-1]
    try:
        return urllib.parse.unquote(name)
    except Exception:
        return name


def split_artist_title(filename):
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r"^\s*\d{1,3}\s*[-–.)\]]+\s*", "", stem)
    for sep in (" - ", " -- ", " -", " – "):
        if sep in stem:
            artist, title = stem.split(sep, 1)
            artist = artist.strip()
            title = title.strip()
            if artist and title and len(artist) <= 80:
                return artist, title
    return "", stem


def norm_key(s):
    s = os.path.splitext(s)[0].lower()
    s = re.sub(r"^(?:\d{1,2}[-_]?)+", "", s)
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


COVER_NAMES = {"cover", "cover1", "coverart", "folder", "front", "frontcover",
               "album", "albumart", "albumartfront", "albumartsmall", "artwork", "art"}
BAD_WORDS = ("back", "inside", "booklet", "inlay", "rear", "spine")


def cover_score(imgname, dirkey):
    ik = norm_key(imgname)
    if not ik:
        return 0
    score = 0
    if dirkey and ik == dirkey:
        score = 100
    elif dirkey and len(ik) >= 5 and ik in dirkey:
        score = 80
    elif dirkey and len(dirkey) >= 5 and dirkey in ik:
        score = 70
    else:
        dt = {t for t in dirkey.split("_") if len(t) > 2} if dirkey else set()
        if dt:
            score = 40 * len(dt & set(ik.split("_"))) // len(dt)
    if ik == "albumartsmall":
        score = max(score, 40)
    elif ik in COVER_NAMES:
        score = max(score, 50)
    elif "front" in ik:
        score = max(score, 45)
    if any(w in ik for w in BAD_WORDS):
        score -= 30
    return max(score, 10)


def pick_cover(image_rels, dirname):
    dirkey = norm_key(dirname)
    best, best_score = None, 0
    for rel in sorted(image_rels):
        s = cover_score(display_name(rel), dirkey)
        if s > best_score:
            best, best_score = rel, s
    return best


def cover_for(sid, rel, is_dir):
    lib = snapshot()
    parts = rel.split("/")
    start = len(parts) if is_dir else len(parts) - 1
    for i in range(start, 0, -1):
        d = "/".join(parts[:i])
        imgs = [c for c in lib["children"].get(srel(sid, d), [])
                if lib["entries"].get(c, {}).get("t") == "i"]
        if imgs:
            best = pick_cover(imgs, display_name(d) if d else "")
            if best:
                return best.split("/", 1)[1]
    return None


def album_art_url(sid, rel, is_dir):
    cover = cover_for(sid, rel, is_dir)
    if not cover:
        return None
    return base_url_of(sid) + cover


def fetch_listing(base, rel=""):
    url = base + rel
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(1 + 2 * attempt)
    raise last


HREF_RE = re.compile(
    r'''<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')''', re.I | re.S)


def parse_listing(page, rel, base_path=""):
    """Return (dir_rels, file_rels, image_rels, display_names) for children of `rel`.

    Handles both relative hrefs (Apache) and root-relative hrefs (LiteSpeed and
    others): an absolute href is mapped back onto the source's base path.
    """
    dirs, files, images = [], [], []
    prefix = rel + "/" if rel else ""
    bp = (base_path or "/").rstrip("/") + "/"
    for match in HREF_RE.finditer(page):
        href = html.unescape(match.group(1) or match.group(2) or "")
        if href.startswith(("?", "#", "mailto:", "http://", "https://")):
            continue
        absolute = href.startswith("/")
        if absolute:
            if not href.startswith(bp):
                continue
            href = href[len(bp):]
        if not href or href in (".", ".."):
            continue
        if ".." in urllib.parse.unquote(href).split("/"):
            continue
        if href.endswith("/"):
            child = href.rstrip("/") if absolute else prefix + href.rstrip("/")
            if child.rstrip("/") in ("", rel):
                continue
            dirs.append(child)
        else:
            child = href if absolute else prefix + href
            ext = os.path.splitext(urllib.parse.unquote(child))[1].lower()
            if ext in AUDIO_EXT:
                files.append(child)
            elif ext in IMAGE_EXT:
                images.append(child)
    names = {}
    for child in dirs + files + images:
        names[child] = display_name(child)
    return dirs, files, images, names


# ---------------------------------------------------------------- sources

def sources_path():
    return os.path.join(DATA_DIR, "sources.json")


def load_sources():
    for path in (sources_path(), os.path.join(INDEX_DIR, "sources.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data and all(
                    s.get("id") and s.get("base_url") for s in data):
                return data
        except (OSError, ValueError):
            pass
    return None


def save_sources():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with _lock:
            data = list(_state["sources"])
        tmp = sources_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, sources_path())
    except OSError as exc:
        log("[sources] save failed:", exc)


def sources():
    with _lock:
        return list(_state["sources"])


def source_by_id(sid):
    for s in sources():
        if s["id"] == sid:
            return s
    return None


def base_url_of(sid):
    s = source_by_id(sid)
    return s["base_url"] if s else ""


def icon_mimetype(icon_url):
    p = urllib.parse.urlparse(icon_url).path.lower()
    if p.endswith(".png"):
        return "image/png"
    if p.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if p.endswith(".gif"):
        return "image/gif"
    if p.endswith(".webp"):
        return "image/webp"
    return "image/vnd.microsoft.icon"


def find_icon(base_url):
    origin = urllib.parse.urlparse(base_url)
    root = "%s://%s/" % (origin.scheme, origin.netloc)
    icon_url = None
    try:
        req = urllib.request.Request(root, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            page = resp.read(200_000).decode("utf-8", "replace")
        for m in re.finditer(r"<link[^>]+>", page, re.I):
            tag = m.group(0)
            if not re.search(r"rel=[\"'][^\"']*(?:icon|shortcut)", tag, re.I):
                continue
            hm = re.search(r"href=[\"']([^\"']+)[\"']", tag, re.I)
            if hm:
                icon_url = urllib.parse.urljoin(root, html.unescape(hm.group(1)))
                break
    except Exception:
        pass
    candidates = [icon_url] if icon_url else []
    candidates += [root + "favicon.ico", root + "favicon.png",
                   root + "apple-touch-icon.png"]
    for candidate in candidates:
        try:
            req = urllib.request.Request(candidate, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if resp.status == 200 and ctype.startswith("image/"):
                    return candidate
        except Exception:
            continue
    return ""


def default_name(url):
    host = urllib.parse.urlparse(url).netloc.split(":")[0]
    host = re.sub(r"^www\.", "", host)
    return host.split(".")[0].capitalize() or "Source"


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "source"


def add_source(name, url):
    name = (name or "").strip()
    if not name:
        raise SourceError("err_name")
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise SourceError("err_scheme")
    if not url.endswith("/"):
        url += "/"
    for s in sources():
        if s["base_url"] == url:
            raise SourceError("err_exists", name=s["name"])
    try:
        page = fetch_listing(url)
    except Exception as exc:
        raise SourceError("err_unreachable", err=exc)
    dirs, files, images, _ = parse_listing(
        page, "", urllib.parse.urlparse(url).path)
    if not (dirs or files or images):
        raise SourceError("err_noindex", url=url)
    base_sid = slugify(name)
    sid = base_sid
    n = 2
    existing = {s["id"] for s in sources()}
    while sid in existing:
        sid = "%s%d" % (base_sid, n)
        n += 1
    icon = find_icon(url)
    with _lock:
        _state["sources"].append({"id": sid, "name": name,
                                  "base_url": url, "icon": icon})
    save_sources()
    log("[sources] added %s (%s) icon=%s" % (name, url, icon))
    threading.Thread(target=build_source, args=(sid,), daemon=True).start()
    return sid


def remove_source(sid):
    with _lock:
        if not any(s["id"] == sid for s in _state["sources"]):
            return False
        _state["sources"] = [s for s in _state["sources"] if s["id"] != sid]
        prefix = sid + "/"
        for k in [k for k in _state["entries"] if k.startswith(prefix)]:
            del _state["entries"][k]
        for k in [k for k in _state["children"] if k.startswith(prefix)]:
            del _state["children"][k]
        _state["building"].pop(sid, None)
    save_sources()
    save_state(force=True)
    log("[sources] removed", sid)
    return True


# ---------------------------------------------------------------- library

def crawl_dir(sid, rel):
    src = source_by_id(sid)
    if not src:
        return []
    try:
        page = fetch_listing(src["base_url"], rel)
    except Exception as exc:
        log("[crawl] %s listing failed %s: %s" % (sid, rel or "/", exc))
        with _lock:
            _state["errors"].append("%s/%s" % (sid, rel))
        return []
    dirs, files, images, names = parse_listing(
        page, rel, urllib.parse.urlparse(src["base_url"]).path)
    with _lock:
        key = srel(sid, rel)
        child_keys = []
        for child in dirs:
            ck = srel(sid, child)
            _state["entries"][ck] = {"t": "d", "name": names[child]}
            child_keys.append(ck)
        for child in files:
            ck = srel(sid, child)
            _state["entries"][ck] = {"t": "f", "name": names[child],
                                     "ext": os.path.splitext(urllib.parse.unquote(child))[1].lower()}
            child_keys.append(ck)
        for child in images:
            ck = srel(sid, child)
            _state["entries"][ck] = {"t": "i", "name": names[child],
                                     "ext": os.path.splitext(urllib.parse.unquote(child))[1].lower()}
            child_keys.append(ck)
        child_keys.sort(key=lambda c: (_state["entries"][c]["t"] != "d",
                                       _state["entries"][c]["name"].lower()))
        _state["children"][key] = child_keys
    return dirs


def build_source(sid):
    src = source_by_id(sid)
    if not src:
        return
    t0 = time.time()
    with _lock:
        prefix = sid + "/"
        for k in [k for k in _state["entries"] if k.startswith(prefix)]:
            del _state["entries"][k]
        for k in [k for k in _state["children"] if k.startswith(prefix)]:
            del _state["children"][k]
        _state["building"][sid] = True
        _state["errors"] = [e for e in _state["errors"] if not e.startswith(sid + "/")]
    done = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            queue = [""]
            seen = {""}
            while queue:
                rels = queue[:]
                queue.clear()
                futures = {pool.submit(crawl_dir, sid, rel): rel for rel in rels}
                for fut in concurrent.futures.as_completed(futures):
                    done += 1
                    if done % 200 == 0:
                        with _lock:
                            nd = len(_state["children"])
                        log("[crawl] %s: %d dirs so far" % (sid, nd))
                        save_state()
                    for child in fut.result() or []:
                        if child in seen:
                            continue
                        seen.add(child)
                        queue.append(child)
    finally:
        with _lock:
            _state["building"][sid] = False
            _state["updated"] = int(time.time())
        stats = source_stats().get(sid, {})
        log("[library] %s built %d dirs, %d files in %.1fs, errors=%d"
            % (sid, stats.get("dirs", 0), stats.get("files", 0),
               time.time() - t0, len([e for e in _state["errors"] if e.startswith(sid + "/")])))
        save_state(force=True)


def build_library(sids=None):
    for s in list(sources()):
        if sids is None or s["id"] in sids:
            build_source(s["id"])


def save_state(force=False):
    now = time.time()
    with _lock:
        if not force and now - _last_save[0] < 30:
            return
        _last_save[0] = now
        payload = {
            "format": 2,
            "updated": _state["updated"],
            "entries": dict(_state["entries"]),
            "children": dict(_state["children"]),
            "errors": list(_state["errors"][:200]),
        }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, "library.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        log("[state] save failed:", exc)


def load_state():
    candidates = (
        (os.path.join(DATA_DIR, "library.json"), False),
        (os.path.join(DATA_DIR, "library.json.gz"), True),
        (os.path.join(INDEX_DIR, "library.json.gz"), True),
    )
    for path, compressed in candidates:
        try:
            if compressed:
                fh = gzip.open(path, "rt", encoding="utf-8")
            else:
                fh = open(path, encoding="utf-8")
            with fh:
                data = json.load(fh)
            if data.get("format") != 2:
                continue
            _state["entries"] = data.get("entries") or {}
            _state["children"] = data.get("children") or {}
            _state["updated"] = data.get("updated") or 0
            _state["errors"] = data.get("errors") or []
            if path != candidates[0][0]:
                log("[state] using bundled index:", path)
            return True
        except (OSError, ValueError, EOFError) as exc:
            if path == candidates[0][0]:
                log("[state] no cache:", exc)
    return False


def snapshot():
    with _lock:
        return {
            "sources": [dict(s) for s in _state["sources"]],
            "entries": dict(_state["entries"]),
            "children": dict(_state["children"]),
            "updated": _state["updated"],
            "building": dict(_state["building"]),
            "errors": list(_state["errors"]),
        }


def source_stats():
    lib = snapshot()
    stats = {}
    for s in lib["sources"]:
        sid = s["id"]
        prefix = sid + "/"
        d = f = i = m = 0
        for k, n in lib["entries"].items():
            if k.startswith(prefix):
                t = n.get("t")
                if t == "d":
                    d += 1
                elif t == "f":
                    f += 1
                    if (n.get("ext") or "").lower() == ".mp3":
                        m += 1
                else:
                    i += 1
        stats[sid] = {"dirs": d, "files": f, "mp3": m, "images": i}
    return stats


def media_children(sid, rel):
    lib = snapshot()
    key = srel(sid, rel)
    return [c for c in lib["children"].get(key, [])
            if lib["entries"].get(c, {}).get("t") in ("d", "f")]


# ---------------------------------------------------------------- DIDL

def didl_container(oid, parent, title, child_count=None, art_url=None, icon_url=None):
    cc = ' childCount="%d"' % child_count if child_count is not None else ""
    extra = ""
    cls = "object.container.storageFolder"
    if icon_url:
        extra += '<res protocolInfo="http-get:*:%s:">%s</res>' % (
            icon_mimetype(icon_url), xml_escape(icon_url))
    if art_url:
        cls = "object.container.musicAlbum"
        extra += "<upnp:albumArtURI>%s</upnp:albumArtURI>" % xml_escape(art_url)
    return (
        '<container id="%s" parentID="%s" restricted="1"%s>'
        "<dc:title>%s</dc:title>"
        "%s"
        "<upnp:class>%s</upnp:class>"
        "</container>" % (xml_escape(oid), xml_escape(parent), cc, xml_escape(title), extra, cls)
    )


def didl_source_container(s):
    return didl_container(s["id"], "0", s["name"],
                          child_count=len(media_children(s["id"], "")),
                          icon_url=s.get("icon") or None)


def didl_file(oid, parent, sid, rel):
    lib = snapshot()
    node = lib["entries"].get(srel(sid, rel))
    if not node or node.get("t") != "f":
        return None
    name = node.get("name") or display_name(rel)
    title = os.path.splitext(name)[0]
    artist, title2 = split_artist_title(name)
    title = title2 or title
    mime, pn = AUDIO_EXT.get(node.get("ext") or ".mp3", ("audio/mpeg", "MP3"))
    album_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    album = display_name(album_rel) if album_rel else ""
    parts = ['<item id="%s" parentID="%s" restricted="1">' % (xml_escape(oid), xml_escape(parent))]
    parts.append("<dc:title>%s</dc:title>" % xml_escape(title))
    if artist:
        parts.append("<dc:creator>%s</dc:creator>" % xml_escape(artist))
        parts.append("<upnp:artist>%s</upnp:artist>" % xml_escape(artist))
    if album:
        parts.append("<upnp:album>%s</upnp:album>" % xml_escape(album))
    art = album_art_url(sid, rel, False)
    if art:
        parts.append("<upnp:albumArtURI>%s</upnp:albumArtURI>" % xml_escape(art))
    parts.append("<upnp:class>object.item.audioItem.musicTrack</upnp:class>")
    proto = "http-get:*:%s:" % mime
    if pn:
        proto += "DLNA.ORG_PN=%s;DLNA.ORG_OP=01" % pn
    parts.append('<res protocolInfo="%s">%s</res>'
                 % (xml_escape(proto), xml_escape(base_url_of(sid) + rel)))
    parts.append("</item>")
    return "".join(parts)


def didl_doc(inner):
    return '<?xml version="1.0"?><DIDL-Lite %s>%s</DIDL-Lite>' % (DIDL_NS, inner)


def parent_oid_of(sid, rel):
    if "/" not in rel:
        return sid
    return "%s/d/%s" % (sid, rel.rsplit("/", 1)[0])


def browse_children(oid, start, count):
    lib = snapshot()
    if oid == "0":
        items = [didl_source_container(s) for s in lib["sources"]]
        return items, len(items)
    parts = oid.split("/", 2)
    sid = parts[0]
    if not source_by_id(sid):
        return None
    if len(parts) == 1:
        rel = ""
        parent_oid = sid
    elif len(parts) == 3 and parts[1] == "d":
        rel = parts[2]
        if srel(sid, rel) not in lib["children"]:
            return None
        parent_oid = oid
    else:
        return None
    kids = media_children(sid, rel)
    items = []
    for child in kids:
        node = lib["entries"].get(child) or {}
        child_rel = child.split("/", 1)[1]
        if node.get("t") == "d":
            items.append(didl_container("%s/d/%s" % (sid, child_rel), parent_oid,
                                        node.get("name") or display_name(child_rel),
                                        len(media_children(sid, child_rel)),
                                        art_url=album_art_url(sid, child_rel, True)))
        else:
            item = didl_file("%s/f/%s" % (sid, child_rel), parent_oid, sid, child_rel)
            if item:
                items.append(item)
    if count > 0:
        return items[start:start + count], len(items)
    return items, len(items)


def browse_metadata(oid):
    lib = snapshot()
    if oid == "0":
        return didl_container("0", "-1", DLNA_NAME)
    parts = oid.split("/", 2)
    sid = parts[0]
    if not source_by_id(sid):
        return None
    if len(parts) == 1:
        s = source_by_id(sid)
        return didl_source_container(s)
    if len(parts) == 3:
        kind, rel = parts[1], parts[2]
        if kind == "d" and srel(sid, rel) in lib["children"]:
            return didl_container(oid, parent_oid_of(sid, rel),
                                  display_name(rel), len(media_children(sid, rel)),
                                  art_url=album_art_url(sid, rel, True) if rel else None)
        if kind == "f":
            return didl_file(oid, parent_oid_of(sid, rel), sid, rel)
    return None


def search_tracks(criteria):
    m = re.match(r'^\s*(dc:title|upnp:artist|upnp:album)\s*="(.*)"\s*$', criteria or "", re.I)
    if not m:
        return [], 0
    key, term = m.group(1).lower(), m.group(2).strip().lower()
    if not term:
        return [], 0
    lib = snapshot()
    out = []
    for k, node in lib["entries"].items():
        if node.get("t") != "f":
            continue
        sid, _, rel = k.partition("/")
        name = node.get("name") or display_name(rel)
        title = os.path.splitext(name)[0]
        artist, _ = split_artist_title(name)
        album_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
        album = display_name(album_rel) if album_rel else ""
        hay = {"dc:title": title, "upnp:artist": artist, "upnp:album": album}.get(key, "")
        if term in hay.lower():
            item = didl_file("%s/f/%s" % (sid, rel), parent_oid_of(sid, rel), sid, rel)
            if item:
                out.append(item)
                if len(out) >= 100:
                    break
    return out, len(out)


# ---------------------------------------------------------------- SCPD

CD_SCPD = (
    '<?xml version="1.0"?>'
    '<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
    "<specVersion><major>1</major><minor>0</minor></specVersion>"
    "<actionList>"
    "<action><name>Browse</name><argumentList>"
    "<argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>"
    "<argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>"
    "<argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>"
    "<argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>"
    "<argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>"
    "<argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>"
    "<argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>"
    "<argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>"
    "<argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>"
    "<argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>"
    "</argumentList></action>"
    "<action><name>Search</name><argumentList>"
    "<argument><name>ContainerID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>"
    "<argument><name>SearchCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SearchCriteria</relatedStateVariable></argument>"
    "<argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>"
    "<argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>"
    "<argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>"
    "<argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>"
    "<argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>"
    "<argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>"
    "<argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>"
    "<argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>"
    "</argumentList></action>"
    "<action><name>GetSortCapabilities</name><argumentList>"
    "<argument><name>SortCaps</name><direction>out</direction><relatedStateVariable>SortCapabilities</relatedStateVariable></argument>"
    "</argumentList></action>"
    "<action><name>GetSearchCapabilities</name><argumentList>"
    "<argument><name>SearchCaps</name><direction>out</direction><relatedStateVariable>SearchCapabilities</relatedStateVariable></argument>"
    "</argumentList></action>"
    "<action><name>GetSystemUpdateID</name><argumentList>"
    "<argument><name>Id</name><direction>out</direction><relatedStateVariable>SystemUpdateID</relatedStateVariable></argument>"
    "</argumentList></action>"
    "</actionList>"
    "<serviceStateTable>"
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType><allowedValueList><allowedValue>BrowseMetadata</allowedValue><allowedValue>BrowseDirectChildren</allowedValue></allowedValueList></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_SearchCriteria</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>SortCapabilities</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>SearchCapabilities</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType></stateVariable>'
    "</serviceStateTable></scpd>\n"
)

CM_SCPD = (
    '<?xml version="1.0"?>'
    '<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
    "<specVersion><major>1</major><minor>0</minor></specVersion>"
    "<actionList>"
    "<action><name>GetProtocolInfo</name><argumentList>"
    "<argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>"
    "<argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>"
    "</argumentList></action>"
    "<action><name>GetCurrentConnectionIDs</name><argumentList>"
    "<argument><name>ConnectionIDs</name><direction>out</direction><relatedStateVariable>CurrentConnectionIDs</relatedStateVariable></argument>"
    "</argumentList></action>"
    "<action><name>GetCurrentConnectionInfo</name><argumentList>"
    "<argument><name>ConnectionID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>"
    "<argument><name>RcsID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_RcsID</relatedStateVariable></argument>"
    "<argument><name>AVTransportID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_AVTransportID</relatedStateVariable></argument>"
    "<argument><name>ProtocolInfo</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ProtocolInfo</relatedStateVariable></argument>"
    "<argument><name>PeerConnectionManager</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionManager</relatedStateVariable></argument>"
    "<argument><name>PeerConnectionID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>"
    "<argument><name>Direction</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Direction</relatedStateVariable></argument>"
    "<argument><name>Status</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionStatus</relatedStateVariable></argument>"
    "</argumentList></action>"
    "</actionList>"
    "<serviceStateTable>"
    '<stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="yes"><name>CurrentConnectionIDs</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionStatus</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionManager</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_Direction</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_ProtocolInfo</name><dataType>string</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionID</name><dataType>i4</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_AVTransportID</name><dataType>i4</dataType></stateVariable>'
    '<stateVariable sendEvents="no"><name>A_ARG_TYPE_RcsID</name><dataType>i4</dataType></stateVariable>'
    "</serviceStateTable></scpd>\n"
)


# ---------------------------------------------------------------- downloads

DL_DIR = (os.environ.get("FRADGIFY_MUSIC", "/music").rstrip("/") or "/music")
DL_HISTORY = os.path.join(DATA_DIR, "download_history.json")
DL_WORKERS = max(1, min(8, WORKERS))
_dl_lock = threading.Lock()
_dl_jobs = []
_dl_seq = [0]
_dl_worker_on = [False]
_dl_history = {}
_dl_hist_loaded = [False]
_dl_hist_last = [0.0]


class _DLCancel(Exception):
    pass


def _dl_hist_load():
    if _dl_hist_loaded[0]:
        return
    with _dl_lock:
        if _dl_hist_loaded[0]:
            return
        try:
            with open(DL_HISTORY, encoding="utf-8") as fh:
                data = json.load(fh)
            _dl_history.update(data.get("files") or {})
            log("[download] history loaded: %d files" % len(_dl_history))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log("[download] history load failed:", exc)
        _dl_hist_loaded[0] = True


def _dl_hist_save(force=False):
    now = time.time()
    with _dl_lock:
        if not force and now - _dl_hist_last[0] < 20:
            return
        _dl_hist_last[0] = now
        payload = {"format": 1, "files": dict(_dl_history)}
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = DL_HISTORY + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, DL_HISTORY)
    except Exception as exc:
        log("[download] history save failed:", exc)


def _dl_record(url, job, dest, size):
    entry = {"url": url, "track": os.path.basename(dest),
             "name": job["name"], "sid": job["sid"], "source": job["source"],
             "rel": job["rel"], "dest": dest, "size": int(size or 0),
             "date": int(time.time())}
    with _dl_lock:
        _dl_history[url] = entry
    _dl_hist_save()


def dl_history(limit=200):
    _dl_hist_load()
    with _dl_lock:
        entries = list(_dl_history.values())
    entries.sort(key=lambda e: e.get("date", 0), reverse=True)
    return len(entries), entries[:limit]


def _dl_name(rel):
    base = rel.rstrip("/").rsplit("/", 1)[-1]
    name = urllib.parse.unquote(base).replace("/", "_").replace("\\", "_").strip()
    return name if name not in ("", ".", "..") else "download"


def _dl_safe(root, inner):
    parts = []
    for part in urllib.parse.unquote(inner).replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("unsafe path")
        parts.append(part)
    return os.path.join(root, *parts)


def _dl_parse(page, rel, base_path=""):
    dirs, files = [], []
    prefix = rel + "/" if rel else ""
    bp = (base_path or "/").rstrip("/") + "/"
    for match in HREF_RE.finditer(page):
        href = html.unescape(match.group(1) or match.group(2) or "")
        if href.startswith(("?", "#", "mailto:", "http://", "https://")):
            continue
        absolute = href.startswith("/")
        if absolute:
            if not href.startswith(bp):
                continue
            href = href[len(bp):]
        if not href or href in (".", ".."):
            continue
        if ".." in urllib.parse.unquote(href).split("/"):
            continue
        if href.endswith("/"):
            child = (href.rstrip("/") if absolute else prefix + href.rstrip("/")).rstrip("/")
            if child and child != rel:
                dirs.append(child)
        else:
            files.append(href if absolute else prefix + href)
    return dirs, files


def _dl_key(path):
    return urllib.parse.unquote(path or "").strip("/")


def _dl_tail(fr, rel):
    fn = _dl_key(fr)
    rn = _dl_key(rel)
    if rn and fn.startswith(rn + "/"):
        return fn[len(rn) + 1:]
    return fn


def _dl_collect(src, rel, job):
    found = []
    found_keys = set()
    rel_n = _dl_key(rel)
    queue = [rel]
    seen = {rel_n}
    while queue and not job["cancel"]:
        batch, queue = queue, []
        for cur in batch:
            if job["cancel"]:
                break
            try:
                page = fetch_listing(src["base_url"], cur)
            except Exception as exc:
                log("[download] %s listing failed %s: %s" % (job["name"], cur or "/", exc))
                with _dl_lock:
                    job["failed"] += 1
                    if len(job["errors"]) < 20:
                        job["errors"].append("%s: %s" % (cur or "/", exc))
                continue
            dirs, files = _dl_parse(
                page, cur, urllib.parse.urlparse(src["base_url"]).path)
            for d in dirs:
                dn = _dl_key(d)
                if rel_n and dn != rel_n and not dn.startswith(rel_n + "/"):
                    continue
                if dn in seen:
                    continue
                seen.add(dn)
                queue.append(d)
            for f in files:
                fn = _dl_key(f)
                if rel_n and not (fn == rel_n or fn.startswith(rel_n + "/")):
                    continue
                if fn in found_keys:
                    continue
                found_keys.add(fn)
                found.append(f)
            with _dl_lock:
                job["total"] = len(found)
    return found


def _dl_one(job, url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        length = resp.headers.get("Content-Length")
        if length is not None:
            length = int(length)
            if os.path.exists(dest) and os.path.getsize(dest) == length:
                return False, length
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            while True:
                if job["cancel"]:
                    raise _DLCancel()
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                with _dl_lock:
                    job["bytes"] += len(chunk)
    os.replace(tmp, dest)
    return True, os.path.getsize(dest)


def _dl_task(job, url, dest):
    if job["cancel"]:
        return None
    last = None
    for attempt in range(3):
        try:
            _downloaded, size = _dl_one(job, url, dest)
            _dl_record(url, job, dest, size)
            return None
        except _DLCancel:
            raise
        except Exception as exc:
            last = exc
            if job["cancel"]:
                return None
            time.sleep(1 + attempt)
    return "%s: %s" % (url, last)


def _dl_run(job):
    src = source_by_id(job["sid"])
    job["state"] = "running"
    job["started"] = int(time.time())
    try:
        if src is None:
            raise RuntimeError("unknown source")
        rel = job["rel"]
        root = os.path.join(DL_DIR, job["name"])
        files = _dl_collect(src, rel, job)
        if job["cancel"]:
            raise _DLCancel()
        os.makedirs(root, exist_ok=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=DL_WORKERS) as pool:
            futs = {}
            for fr in files:
                if job["cancel"]:
                    break
                url = src["base_url"] + fr
                if url in _dl_history:
                    with _dl_lock:
                        job["done"] += 1
                        job["skipped"] += 1
                    continue
                inner = _dl_tail(fr, rel)
                try:
                    dest = _dl_safe(root, inner)
                except ValueError as exc:
                    with _dl_lock:
                        job["failed"] += 1
                        if len(job["errors"]) < 20:
                            job["errors"].append("%s: %s" % (fr, exc))
                    continue
                futs[pool.submit(_dl_task, job, url, dest)] = fr
            for fut in concurrent.futures.as_completed(futs):
                err = fut.result()
                with _dl_lock:
                    job["done"] += 1
                    if err:
                        job["failed"] += 1
                        if len(job["errors"]) < 20:
                            job["errors"].append(err)
        job["state"] = "cancelled" if job["cancel"] else "done"
    except _DLCancel:
        job["state"] = "cancelled"
    except Exception as exc:
        job["state"] = "error"
        with _dl_lock:
            if len(job["errors"]) < 20:
                job["errors"].append(str(exc))
        log("[download] %s failed: %s" % (job["name"], exc))
    finally:
        job["finished"] = int(time.time())
        _dl_hist_save(force=True)
        log("[download] %s %s: %d/%d files (%d known), %d bytes, %d errors"
            % (job["name"], job["state"], job["done"], job["total"],
               job["skipped"], job["bytes"], job["failed"]))


def _dl_worker():
    while True:
        with _dl_lock:
            job = next((j for j in _dl_jobs if j["state"] == "queued"), None)
            if job is None:
                _dl_worker_on[0] = False
                job = next((j for j in _dl_jobs if j["state"] == "queued"), None)
                if job is None:
                    return
                _dl_worker_on[0] = True
        _dl_run(job)


def dl_enqueue(sid, rel):
    src = source_by_id(sid)
    if not src:
        return None
    _dl_hist_load()
    rel_enc = urllib.parse.quote(urllib.parse.unquote(rel or "").strip("/"), safe="/")
    name = _dl_name(rel_enc)
    with _dl_lock:
        for j in _dl_jobs:
            if j["sid"] == sid and j["rel"] == rel_enc and j["state"] in ("queued", "running"):
                return j
        _dl_seq[0] += 1
        job = {"id": _dl_seq[0], "sid": sid, "source": src["name"],
               "rel": rel_enc, "name": name, "state": "queued", "total": 0,
               "done": 0, "failed": 0, "skipped": 0, "bytes": 0, "errors": [],
               "cancel": False, "started": 0, "finished": 0}
        _dl_jobs.insert(0, job)
        while len(_dl_jobs) > 50:
            for i in range(len(_dl_jobs) - 1, -1, -1):
                if _dl_jobs[i]["state"] not in ("queued", "running"):
                    del _dl_jobs[i]
                    break
            else:
                break
        start = not _dl_worker_on[0]
        if start:
            _dl_worker_on[0] = True
    if start:
        threading.Thread(target=_dl_worker, daemon=True).start()
    return job


def dl_cancel(job_id):
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return False
    with _dl_lock:
        for j in _dl_jobs:
            if j["id"] == job_id and j["state"] in ("queued", "running"):
                j["cancel"] = True
                if j["state"] == "queued":
                    j["state"] = "cancelled"
                    j["finished"] = int(time.time())
                return True
    return False


def dl_jobs():
    with _dl_lock:
        return [{"id": j["id"], "sid": j["sid"], "source": j["source"],
                 "rel": j["rel"], "name": j["name"], "state": j["state"],
                 "total": j["total"], "done": j["done"], "failed": j["failed"],
                 "skipped": j["skipped"], "bytes": j["bytes"],
                 "errors": list(j["errors"]),
                 "started": j["started"], "finished": j["finished"]}
                for j in _dl_jobs]


def device_xml(udn):
    base = base_url()
    return (
        '<?xml version="1.0"?>\n'
        '<root xmlns="urn:schemas-upnp-org:device-1-0" '
        'xmlns:dlna="urn:schemas-dlna-org:device-1-0" configId="1">'
        "<specVersion><major>1</major><minor>0</minor></specVersion>"
        "<device>"
        "<deviceType>%s</deviceType>"
        "<friendlyName>%s</friendlyName>"
        "<manufacturer>hans.hauberger@gmail.com</manufacturer>"
        "<modelDescription>web-dlna DLNA media server</modelDescription>"
        "<modelName>web-dlna-server</modelName>"
        "<modelNumber>%s</modelNumber>"
        "<serialNumber>1</serialNumber>"
        "<UDN>uuid:%s</UDN>"
        "<dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC>"
        "<iconList>"
        "<icon><mimetype>image/jpeg</mimetype><width>150</width><height>150</height>"
        "<depth>24</depth><url>%s/dlna/icon.jpg</url></icon>"
        "<icon><mimetype>image/jpeg</mimetype><width>64</width><height>64</height>"
        "<depth>24</depth><url>%s/dlna/icon64.jpg</url></icon>"
        "</iconList>"
        "<serviceList>"
        "<service><serviceType>%s</serviceType>"
        "<serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>"
        "<SCPDURL>/dlna/ContentDirectory.xml</SCPDURL>"
        "<controlURL>/dlna/ContentDirectory/control</controlURL>"
        "<eventSubURL>/dlna/ContentDirectory/event</eventSubURL></service>"
        "<service><serviceType>%s</serviceType>"
        "<serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>"
        "<SCPDURL>/dlna/ConnectionManager.xml</SCPDURL>"
        "<controlURL>/dlna/ConnectionManager/control</controlURL>"
        "<eventSubURL>/dlna/ConnectionManager/event</eventSubURL></service>"
        "</serviceList>"
        "</device></root>\n"
    ) % (UPNP_DEVICE_TYPE, xml_escape(DLNA_NAME), VERSION, udn, base, base, CD_SERVICE, CM_SERVICE)


# ---------------------------------------------------------------- SSDP

class Ssdp(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="ssdp")
        self.ip = DLNA_IP or detect_ip()
        self.uuid = device_uuid()
        self.location = "http://%s:%d/dlna/device.xml" % (self.ip, PORT)
        self.stop_event = threading.Event()
        self.sock = None
        self.subscribers = {}
        self.sid = 0

    def run(self):
        global DLNA_BASE
        DLNA_BASE = "http://%s:%d" % (self.ip, PORT)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (OSError, AttributeError):
                pass
            sock.bind(("", SSDP_PORT))
            mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton(self.ip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(1.0)
            self.sock = sock
        except OSError as exc:
            log("[ssdp] cannot bind 1900:", exc)
        log("[ssdp] location", self.location, "uuid", self.uuid)
        self.notify("ssdp:alive")
        last = time.time()
        while not self.stop_event.is_set():
            if self.sock is not None:
                try:
                    data, addr = self.sock.recvfrom(8192)
                    self.handle(data, addr)
                except socket.timeout:
                    pass
                except OSError:
                    break
            else:
                self.stop_event.wait(1)
            if time.time() - last >= DLNA_NOTIFY:
                last = time.time()
                self.notify("ssdp:alive")
        self.notify("ssdp:byebye")

    def notify(self, nts):
        sock = self.sock
        if sock is None:
            return
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        except OSError:
            pass
        for target in ["upnp:rootdevice"] + UPNP_TARGETS:
            usn = "uuid:%s::%s" % (self.uuid, target)
            lines = [
                "NOTIFY * HTTP/1.1",
                "HOST: %s:%d" % (SSDP_ADDR, SSDP_PORT),
                "NT: %s" % target,
                "NTS: %s" % nts,
                "USN: %s" % usn,
            ]
            if nts == "ssdp:alive":
                lines += [
                    "LOCATION: %s" % self.location,
                    "CACHE-CONTROL: max-age=1800",
                    "SERVER: %s" % SSDP_SERVER,
                ]
            try:
                sock.sendto(("\r\n".join(lines) + "\r\n\r\n").encode(), (SSDP_ADDR, SSDP_PORT))
            except OSError:
                pass

    def handle(self, data, addr):
        text = data.decode("utf-8", "replace")
        lines = text.split("\r\n")
        if not lines or not lines[0].startswith("M-SEARCH"):
            return
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().upper()] = v.strip()
        st = headers.get("ST", "")
        if st not in ("ssdp:all", "upnp:rootdevice") and st not in UPNP_TARGETS:
            return
        try:
            mx = int(headers.get("MX", "1"))
        except ValueError:
            mx = 1
        threading.Thread(target=self.reply, args=(st, mx, addr), daemon=True).start()

    def reply(self, st, mx, addr):
        time.sleep(random.uniform(0, min(max(mx, 0), 3)))
        targets = ["upnp:rootdevice"] + UPNP_TARGETS if st == "ssdp:all" else [st]
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        except OSError:
            return
        for target in targets:
            usn = "uuid:%s::%s" % (self.uuid, target)
            msg = (
                "HTTP/1.1 200 OK\r\n"
                "CACHE-CONTROL: max-age=1800\r\n"
                "DATE: %s\r\n"
                "EXT:\r\n"
                "LOCATION: %s\r\n"
                "SERVER: %s\r\n"
                "ST: %s\r\n"
                "USN: %s\r\n\r\n"
            ) % (time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
               self.location, SSDP_SERVER, target, usn)
            try:
                sock.sendto(msg.encode(), addr)
            except OSError:
                pass
        sock.close()

    def subscribe(self, service, callback):
        self.sid += 1
        sid = "uuid:%s" % uuid.uuid4()
        self.subscribers[sid] = {"service": service, "callback": callback}
        return sid


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "web-dlna/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def send_bytes(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_body(self):
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            out = []
            while True:
                line = self.rfile.readline(65536).strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline(65536)
                    break
                out.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(out)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def form_fields(self):
        raw = self.read_body()
        return {k: v[0] if v else ""
                for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}

    def do_HEAD(self):
        self.route()

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_SUBSCRIBE(self):
        self.route()

    def do_UNSUBSCRIBE(self):
        self.route()

    def route(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path in ("/", "/index.html"):
                return self.dashboard()
            if path in ("/demo/player", "/player-demo.html"):
                return self.demo_player()
            if path in ("/playlist.m3u", "/demo/playlist.m3u"):
                return self.playlist_m3u(parsed.query)
            if path == "/genres" or path.startswith("/genres/"):
                return self.genres(path)
            if path == "/downloads":
                return self.downloads_page()
            if path == "/download":
                return self.download_start(parsed.query)
            if path == "/download/cancel":
                return self.download_cancel(parsed.query)
            if path == "/api/downloads":
                return self.downloads_api()
            if path == "/api/download" and self.command == "POST":
                return self.download_start(parsed.query)
            if path in ("/admin", "/admin/"):
                if self.command == "POST":
                    return self.admin_post()
                return self.admin(parsed.query)
            if path == "/health":
                return self.send_bytes(200, {"ok": True, "version": VERSION})
            if path == "/api/summary":
                return self.summary()
            if path == "/api/refresh" and self.command == "POST":
                q = urllib.parse.parse_qs(parsed.query or "")
                sids = [x for x in (q.get("sid") or []) if x]
                threading.Thread(target=build_library,
                                 args=(sids or None,), daemon=True).start()
                return self.send_bytes(202, {"status": "refreshing",
                                             "sources": sids or "all"})
            if path.startswith("/dlna/"):
                if path.startswith("/dlna/ContentDirectory/control"):
                    return self.soap(CD_SERVICE, "cd")
                if path.startswith("/dlna/ConnectionManager/control"):
                    return self.soap(CM_SERVICE, "cm")
                if path.endswith("/event"):
                    return self.event(path)
                if path == "/dlna/device.xml":
                    return self.send_bytes(
                        200,
                        device_xml(_dlna_server.uuid if _dlna_server else device_uuid()),
                        'text/xml; charset="utf-8"')
                if path in ("/dlna/icon.jpg", "/dlna/icon64.jpg"):
                    return self.icon(os.path.basename(path))
                if path == "/dlna/ContentDirectory.xml":
                    return self.send_bytes(200, CD_SCPD, 'text/xml; charset="utf-8"')
                if path == "/dlna/ConnectionManager.xml":
                    return self.send_bytes(200, CM_SCPD, 'text/xml; charset="utf-8"')
                if path == "/dlna/status":
                    srv = _dlna_server
                    return self.send_bytes(200, {
                        "name": DLNA_NAME,
                        "uuid": srv.uuid if srv else None,
                        "location": srv.location if srv else None,
                    })
            return self.send_bytes(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log("[http] error", self.path, exc)
            try:
                self.send_bytes(500, {"error": str(exc)})
            except Exception:
                pass

    def style(self):
        return ("body{font-family:system-ui,sans-serif;margin:24px 32px;"
                "line-height:1.6;color:#222}code{background:#eee;padding:2px 5px}"
                "h1{font-size:1.4em}table{border-collapse:collapse;width:100%}"
                "th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #ddd}"
                "a.button{display:inline-block;margin-top:12px;padding:8px 14px;background:#2563eb;"
                "color:#fff;text-decoration:none;border-radius:6px}"
                "a.danger{padding:4px 10px;background:#dc2626;color:#fff;text-decoration:none;"
                "border-radius:5px;font-size:0.9em}form.add{margin-top:24px;padding:16px;"
                "background:#f6f8fa;border-radius:8px}form.add input{padding:8px;margin:4px 8px 4px 0}"
                ".msg{padding:8px 12px;border-radius:6px;margin:12px 0}"
                ".ok{background:#dcfce7;color:#14532d;font-weight:700}"
                ".err{background:#fee2e2;color:#991b1b}"
                "tr.summe td,tr.summe th{font-weight:700;border-top:2px solid #999;background:#f1f5f9}"
                "th.mp3,td.mp3,tr.summe td.mp3{background:#dcfce7;color:#14532d;font-weight:700}"
                ".st{display:inline-block;padding:2px 10px;border-radius:999px;"
                "font-size:.85em;font-weight:700;white-space:nowrap}"
                ".st-ok{background:#dcfce7;color:#14532d;cursor:pointer;"
                "text-decoration:none}.st-ok:hover{background:#bbf7d0}"
                ".st-build{background:#ffedd5;color:#9a3412}"
                ".st-err{background:#fee2e2;color:#991b1b;cursor:pointer;"
                "text-decoration:none}.st-err:hover{background:#fecaca}"
                ".pill{display:inline-block;padding:4px 12px;border-radius:999px;"
                "background:#e0f2fe;color:#075985;font-weight:700;font-size:.9em}"
                "td.mp3 a{color:inherit;text-decoration:none}"
                "td.mp3 a:hover{text-decoration:underline}"
                "table.genres{width:auto;min-width:320px}"
                "a.dl{display:inline-block;padding:2px 10px;background:#2563eb;"
                "color:#fff;border-radius:6px;font-size:.85em;text-decoration:none}"
                "a.dl:hover{background:#1d4ed8}"
                "a.dl2{display:inline-block;padding:2px 8px;background:#e5e7eb;"
                "color:#111;border-radius:6px;font-size:.8em;text-decoration:none}"
                "a.dl2:hover{background:#d1d5db}"
                "button.mini{padding:2px 10px;border:1px solid #dc2626;background:#fff;"
                "color:#dc2626;border-radius:6px;cursor:pointer}"
                "button.mini:hover{background:#fee2e2}"
                "tr.dl-error td{background:#fef2f2}tr.dl-running td{background:#eff6ff}"
                "table.hist td{font-size:.85em}table.hist td:first-child{max-width:460px;"
                "overflow-wrap:anywhere}"
                "form.lang{margin-top:16px;padding:16px;background:#f6f8fa;border-radius:8px}"
                "form.lang label{display:block}form.lang select{padding:8px;font-size:1em;"
                "width:100%;box-sizing:border-box}")

    def playlist_m3u(self, query):
        q = urllib.parse.parse_qs(query or "")
        try:
            start = max(0, int((q.get("start") or ["0"])[0]))
        except ValueError:
            start = 0
        try:
            limit = int((q.get("limit") or ["12"])[0])
        except ValueError:
            limit = 12
        limit = max(1, min(limit, 200))
        random_pick = (q.get("random") or [""])[0] in ("1", "true", "yes")
        sid_f = (q.get("sid") or [""])[0]
        g_f = (q.get("g") or [""])[0].strip("/")
        want_all = (q.get("all") or [""])[0] in ("1", "true", "yes")
        lib = snapshot()
        sources = {s["id"]: s for s in lib["sources"]}
        tracks = []
        for key in sorted(lib["entries"]):
            node = lib["entries"][key]
            if node.get("t") != "f" or (node.get("ext") or ".mp3").lower() != ".mp3":
                continue
            sid, _, rel = key.partition("/")
            src = sources.get(sid)
            if not src:
                continue
            if sid_f and sid != sid_f:
                continue
            if g_f:
                rel_dec = urllib.parse.unquote(rel)
                if rel_dec != g_f and not rel_dec.startswith(g_f + "/"):
                    continue
            tracks.append((sid, rel, node, src))
        total = len(tracks)
        if want_all:
            limit = min(total, 20000)
        if random_pick:
            picked = random.sample(tracks, min(limit, total)) if total else []
            start = 0
        else:
            picked = tracks[start:start + limit]
        lines = ["#EXTM3U"]
        for sid, rel, node, src in picked:
            name = urllib.parse.unquote(node.get("name") or display_name(rel))
            artist, title = split_artist_title(name)
            label = ("%s - %s" % (artist, title)) if artist else (title or name)
            album_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if album_rel:
                label = "%s · %s" % (display_name(album_rel), label)
            lines.append("#EXTINF:-1,%s" % label.replace("\n", " "))
            lines.append(src["base_url"] + rel)
        body = "\n".join(lines) + "\n"
        return self.send_bytes(200, body.encode("utf-8"), "audio/x-mpegurl; charset=utf-8", {
            "X-Total-Count": str(total),
            "X-Start": str(start),
            "X-Limit": str(limit),
            "X-Returned": str(len(picked)),
        })

    def genres(self, path):
        lib = snapshot()
        lang = request_lang(self)
        tr = T[lang]
        parts = path.split("/", 3)
        sid = parts[2] if len(parts) > 2 else ""
        rel = parts[3] if len(parts) > 3 else ""
        if not sid:
            rows = "".join(
                "<tr><td><a href='/genres/%s'>%s</a></td></tr>"
                % (urllib.parse.quote(x["id"]), xml_escape(x["name"]))
                for x in lib["sources"])
            body = ("<!doctype html><html lang='%s'><head><meta charset=utf-8>"
                    "<link rel='icon' type='image/jpeg' href='/dlna/icon64.jpg'>"
                    "<title>web-dlna</title><style>%s</style></head><body>"
                    "<h1><img src='/dlna/icon.jpg' width='42' height='42' "
                    "style='vertical-align:middle'> web-dlna</h1>"
                    "<h2>%s</h2><table class=genres>%s</table>"
                    "<p><a class=button href='/'>%s</a></p></body></html>"
                    % (lang, self.style(), xml_escape(tr["th_source"]),
                       rows, tr["back"]))
            return self.send_bytes(200, body, "text/html; charset=utf-8")
        src = next((x for x in lib["sources"] if x["id"] == sid), None)
        if not src:
            return self.send_bytes(404, {"error": "unknown source"})
        pfx = sid + "/" + ((rel + "/") if rel else "")
        groups = {}
        for key, node in lib["entries"].items():
            if not key.startswith(pfx):
                continue
            sub = key[len(pfx):]
            if not sub:
                continue
            t = node.get("t")
            if "/" in sub or t == "d":
                name = sub.split("/", 1)[0]
                is_dir = True
            else:
                name = tr["no_genre"]
                is_dir = False
            g = groups.get(name)
            if g is None:
                g = groups[name] = {"name": name, "dir": is_dir,
                                    "mp3": 0, "files": 0}
            if t == "f":
                g["files"] += 1
                if (node.get("ext") or "").lower() == ".mp3":
                    g["mp3"] += 1
        ordered = sorted(groups.values(),
                         key=lambda g: (-g["mp3"],
                                        urllib.parse.unquote(g["name"]).lower()))
        rows = []
        for g in ordered:
            label = xml_escape(urllib.parse.unquote(g["name"]))
            dl = ""
            if g["dir"] and rel:
                href = xml_escape(src["base_url"] + rel + "/" + g["name"] + "/")
                label = ("<a href='%s' target='_blank' rel='noopener noreferrer'>%s</a>"
                         % (href, label))
            elif g["dir"]:
                label = ("<a href='/genres/%s/%s'>%s</a>"
                         % (urllib.parse.quote(sid), g["name"], label))
            if g["dir"]:
                gpath = ((rel + "/") if rel else "") + g["name"]
                fname = html.escape(urllib.parse.unquote(g["name"]), quote=True)
                dl = ("<a class='dl' href='/download?sid=%s&g=%s' title='%s'>%s</a> "
                      "<a class='dl2' href='/playlist.m3u?sid=%s&g=%s&all=1' "
                      "download='%s.m3u' title='M3U'>M3U</a>"
                      % (urllib.parse.quote(sid), m3u_q(gpath),
                         xml_escape(tr["dl_folder"]), xml_escape(tr["dl_folder"]),
                         urllib.parse.quote(sid), m3u_q(gpath), fname))
            rows.append("<tr><td>%s</td><td class=mp3>%s</td><td>%s</td></tr>"
                        % (label, fmt_int(g["mp3"]), dl))
        dl_all = ""
        if not rel:
            dl_all = ("<p><a class='dl' href='/playlist.m3u?sid=%s&all=1' "
                      "download='%s.m3u' title='%s'>%s (M3U)</a></p>"
                      % (urllib.parse.quote(sid),
                         html.escape(src["name"], quote=True),
                         xml_escape(tr["download"]), xml_escape(tr["download"])))
        title = xml_escape(src["name"])
        if rel:
            title += " &rsaquo; " + xml_escape(urllib.parse.unquote(rel))
        back = ("/genres/%s" % urllib.parse.quote(sid)) if rel else "/"
        body = ("<!doctype html><html lang='%s'><head><meta charset=utf-8>"
                "<link rel='icon' type='image/jpeg' href='/dlna/icon64.jpg'>"
                "<title>%s</title><style>%s</style></head><body>"
                "<h1><img src='/dlna/icon.jpg' width='42' height='42' "
                "style='vertical-align:middle'> web-dlna</h1>"
                "<h2>%s</h2>"
                "%s"
                "<table class=genres><tr><th>%s</th><th class=mp3>%s</th>"
                "<th>%s</th></tr>%s</table>"
                "<p><a class=button href='%s'>%s</a></p></body></html>"
                % (lang, title, self.style(), title, dl_all,
                   xml_escape(tr["th_dirs"] if rel else tr["th_genre"]),
                   xml_escape(tr["th_mp3"]), xml_escape(tr["download"]),
                   "".join(rows), back, tr["back"]))
        return self.send_bytes(200, body, "text/html; charset=utf-8")

    def demo_player(self):
        try:
            with open(os.path.join(WEB_DIR, "player-demo.html"), "rb") as fh:
                body = fh.read()
        except OSError:
            return self.send_bytes(404, {"error": "demo page missing"})
        return self.send_bytes(200, body, "text/html; charset=utf-8")

    def download_start(self, query):
        q = urllib.parse.parse_qs(query or "")
        sid = (q.get("sid") or [""])[0]
        rel = (q.get("g") or [""])[0]
        job = dl_enqueue(sid, rel)
        if job is None:
            return self.send_bytes(404, {"error": "unknown source"})
        if self.command == "POST":
            return self.send_bytes(202, {"status": job["state"], "id": job["id"],
                                         "name": job["name"], "dir": DL_DIR})
        return self.redirect("/downloads")

    def download_cancel(self, query):
        q = urllib.parse.parse_qs(query or "")
        if self.command == "POST":
            q.update(self.form_fields())
        ok = dl_cancel((q.get("id") or [""])[0])
        if self.command == "POST":
            return self.redirect("/downloads")
        return self.send_bytes(200 if ok else 404, {"cancelled": ok})

    def downloads_page(self):
        tr = T[request_lang(self)]
        jobs = dl_jobs()
        active = any(j["state"] in ("queued", "running") for j in jobs)
        rows = []
        for j in jobs:
            label = tr.get("dl_" + j["state"], j["state"])
            if j["failed"]:
                label += " (%d %s)" % (j["failed"], tr["st_error"].lower())
            title = html.escape(" | ".join(j["errors"])[:400], quote=True)
            action = ""
            if j["state"] in ("queued", "running"):
                action = ("<form method='post' action='/download/cancel' "
                          "style='display:inline'><input type=hidden name=id value='%d'>"
                          "<button type=submit class=mini>%s</button></form>"
                          % (j["id"], xml_escape(tr["dl_cancel"])))
            when = time.strftime("%d.%m. %H:%M", time.localtime(j["started"] or j["finished"]
                                                                or time.time()))
            files_txt = "%s / %s" % (fmt_int(j["done"]), fmt_int(j["total"]))
            if j["skipped"]:
                files_txt += " (%s %s)" % (fmt_int(j["skipped"]), tr["dl_skipped"])
            rows.append(
                "<tr class='dl-%s'><td>%s<br><small>%s</small></td><td>%s</td>"
                "<td title='%s'>%s</td><td>%s</td><td>%.1f MB</td>"
                "<td><code>%s</code></td><td>%s</td></tr>"
                % (j["state"], xml_escape(j["name"]), when, xml_escape(j["source"]),
                   title, label, files_txt,
                   j["bytes"] / 1048576.0,
                   xml_escape(os.path.join(DL_DIR, j["name"])), action))
        if not rows:
            rows.append("<tr><td colspan=7>%s</td></tr>" % xml_escape(tr["dl_empty"]))
        hist_total, hist = dl_history(200)
        hrows = []
        for e in hist:
            hrows.append(
                "<tr><td><a href='%s' target='_blank' rel='noopener noreferrer'>%s</a></td>"
                "<td>%s</td><td>%s</td><td>%s</td></tr>"
                % (html.escape(e.get("url", ""), quote=True), xml_escape(e.get("url", "")),
                   xml_escape(e.get("track", "")), xml_escape(e.get("name", "")),
                   time.strftime("%d.%m.%Y %H:%M",
                                 time.localtime(e.get("date") or time.time()))))
        if not hrows:
            hrows.append("<tr><td colspan=4>%s</td></tr>" % xml_escape(tr["dl_empty"]))
        history_html = ("<h2>%s (%s)</h2><table class=hist>"
                        "<tr><th>%s</th><th>%s</th><th>%s</th><th>%s</th></tr>%s"
                        "</table>" % (xml_escape(tr["dl_history"]), fmt_int(hist_total),
                                      xml_escape(tr["dl_link"]), xml_escape(tr["dl_track"]),
                                      xml_escape(tr["dl_name"]), xml_escape(tr["dl_date"]),
                                      "".join(hrows)))
        refresh = "<meta http-equiv=refresh content=3>" if active else ""
        try:
            free_bytes = shutil.disk_usage(DL_DIR).free
        except OSError:
            free_bytes = 0
        free_pill = ("<p><span class='pill'>%s</span></p>"
                     % xml_escape(tr["dl_free"] % fmt_bytes(free_bytes)))
        body = (
            "<!doctype html><html lang='%s'><head><meta charset=utf-8>%s"
            "<link rel='icon' type='image/jpeg' href='/dlna/icon64.jpg'>"
            "<title>web-dlna</title><style>%s</style></head><body>"
            "<h1><img src='/dlna/icon.jpg' width='42' height='42' "
            "style='vertical-align:middle'> web-dlna</h1>"
            "%s"
            "<h2>%s</h2><p><code>%s</code></p>"
            "<table><tr><th>%s</th><th>%s</th><th>%s</th><th>%s</th>"
            "<th>%s</th><th>%s</th><th></th></tr>%s</table>%s"
            "<p><a class=button href='/'>%s</a></p></body></html>"
            % (request_lang(self), refresh, self.style(), free_pill,
               xml_escape(tr["dl_jobs"]), xml_escape(DL_DIR),
               xml_escape(tr["dl_folder"]), xml_escape(tr["th_source"]),
               xml_escape(tr["dl_status"]), xml_escape(tr["dl_files"]),
               xml_escape(tr["dl_size"]), xml_escape(tr["dl_dest"]),
               "".join(rows), history_html, xml_escape(tr["back"])))
        return self.send_bytes(200, body, "text/html; charset=utf-8")

    def downloads_api(self):
        jobs = dl_jobs()
        _dl_hist_load()
        return self.send_bytes(200, {"dir": DL_DIR, "jobs": jobs,
                                     "history": len(_dl_history)})

    def dashboard(self):
        lib = snapshot()
        stats = source_stats()
        lang = request_lang(self)
        tr = T[lang]
        updated = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lib["updated"]))
                   if lib["updated"] else tr["never"])
        rows = []
        for pos, s in enumerate(lib["sources"], 1):
            st = stats.get(s["id"], {})
            errs = [e for e in lib["errors"] if e.startswith(s["id"] + "/")]
            building = status_badge(tr, s["id"], lib["building"].get(s["id"]), errs)
            icon = ('<img src="%s" width="24" height="24" style="vertical-align:middle" '
                    'onerror="this.onerror=null;this.src=\'/dlna/icon64.jpg\'"> '
                    % xml_escape(s.get("icon") or "/dlna/icon64.jpg"))
            rows.append("<tr><td>%d. %s<b>%s</b></td><td><a href='%s' target='_blank' rel='noopener noreferrer'><code>%s</code></a></td><td>%s</td>"
                    "<td>%s</td><td class=mp3><a href='/genres/%s'>%s</a></td>"
                    "<td>%s</td><td>%s</td></tr>"
                    % (pos, icon, xml_escape(s["name"]), xml_escape(s["base_url"]),
                       xml_escape(s["base_url"]),
                       fmt_int(st.get("dirs", 0)), fmt_int(st.get("files", 0)),
                       urllib.parse.quote(s["id"]), fmt_int(st.get("mp3", 0)),
                       fmt_int(st.get("images", 0)), building))
        tot = {k: sum(st.get(k, 0) for st in stats.values())
               for k in ("dirs", "files", "mp3", "images")}
        rows.append("<tr class=summe><td></td><td></td><td>%s</td><td>%s</td>"
                    "<td class=mp3>%s</td><td>%s</td><td></td></tr>"
                    % (fmt_int(tot["dirs"]), fmt_int(tot["files"]),
                       fmt_int(tot["mp3"]), fmt_int(tot["images"])))
        body = (
            "<!doctype html><html lang='%s'><head><meta charset=utf-8>"
            "<link rel='icon' type='image/jpeg' href='/dlna/icon64.jpg'>"
            "<title>web-dlna</title><style>%s</style></head><body>"
            "<h1><img src='/dlna/icon.jpg' width='42' height='42' "
            "style='vertical-align:middle'> web-dlna</h1>"
            "<table><tr><th>%s</th><th>%s</th><th>%s</th><th>%s</th>"
            "<th class=mp3>%s</th><th>%s</th><th>%s</th></tr>%s</table>"
            "<a class=button href='/admin'>%s</a> "
            "<a class=button href='/api/refresh' onclick=\"fetch('/api/refresh',{method:'POST'});"
            "return false\">%s</a>"
            "<p><a href='/api/summary' target='_blank' rel='noopener noreferrer'>%s</a> &middot; "
            "<a href='/dlna/device.xml' target='_blank' rel='noopener noreferrer'>%s</a> &middot; "
            "<a href='/downloads'>%s</a> &middot; %s</p>"
            "</body></html>"
        ) % (lang, self.style(),
             tr["th_source"], tr["th_url"], tr["th_dirs"], tr["th_files"],
             tr["th_mp3"], tr["th_images"], tr["th_status"],
             "".join(rows),
             tr["manage"], tr["rescan"], tr["json"], tr["device"],
             tr["dl_jobs"], tr["updated"] % updated)
        return self.send_bytes(200, body, "text/html; charset=utf-8")

    def admin(self, query):
        lib = snapshot()
        stats = source_stats()
        q = urllib.parse.parse_qs(query or "")
        lang_param = (q.get("lang") or [""])[0]
        if lang_param in T:
            lang = lang_param
            cookie = "lang=%s; Path=/; Max-Age=31536000; SameSite=Lax" % lang
        else:
            lang = request_lang(self)
            cookie = None
        tr = T[lang]
        msg = (q.get("msg") or [""])[0]
        ok = (q.get("kind") or ["ok"])[0] == "ok"
        msg_html = ('<div class="msg %s">%s</div>' % ("ok" if ok else "err", xml_escape(msg))) if msg else ""
        lang_select = (
            "<form class=lang method='get' action='/admin'><label>%s: "
            "<select name='lang' onchange='this.form.submit()'>%s</select></label></form>"
            % (tr["lang"], "".join(
                "<option value='%s'%s>%s</option>"
                % (code, " selected" if code == lang else "", LANG_NAMES[code])
                for code in LANGS)))
        rows = []
        for pos, s in enumerate(lib["sources"], 1):
            st = stats.get(s["id"], {})
            errs = [e for e in lib["errors"] if e.startswith(s["id"] + "/")]
            building = status_badge(tr, s["id"], lib["building"].get(s["id"]), errs)
            icon = ('<img src="%s" width="24" height="24" style="vertical-align:middle" '
                    'onerror="this.onerror=null;this.src=\'/dlna/icon64.jpg\'">'
                    % xml_escape(s.get("icon") or "/dlna/icon64.jpg"))
            name_js = xml_escape(s["name"]).replace("'", "\\'")
            rows.append(
                "<tr><td>%d. %s <b>%s</b></td><td><a href='%s' target='_blank' rel='noopener noreferrer'><code>%s</code></a></td><td>%s</td>"
                "<td>%s</td><td class=mp3><a href='/genres/%s'>%s</a></td><td>%s</td>"
                "<td>%s</td><td><form method='post' action='/admin' style='display:inline'>"
                "<input type='hidden' name='action' value='remove'>"
                "<input type='hidden' name='sid' value='%s'>"
                "<button class='danger' style='padding:4px 10px;background:#dc2626;color:#fff;"
                "border:none;border-radius:5px;cursor:pointer' "
                "onclick=\"return confirm('%s')\">%s</button>"
                "</form></td></tr>"
                % (pos, icon, xml_escape(s["name"]),
                   xml_escape(s["base_url"]), xml_escape(s["base_url"]),
                   fmt_int(st.get("dirs", 0)), fmt_int(st.get("files", 0)),
                   urllib.parse.quote(s["id"]), fmt_int(st.get("mp3", 0)),
                   fmt_int(st.get("images", 0)),
                   building, xml_escape(s["id"]),
                   tr["confirm"] % name_js, tr["remove"]))
        tot = {k: sum(st.get(k, 0) for st in stats.values())
               for k in ("dirs", "files", "mp3", "images")}
        rows.append("<tr class=summe><td></td><td></td><td>%s</td><td>%s</td>"
                    "<td class=mp3>%s</td><td>%s</td><td></td><td></td></tr>"
                    % (fmt_int(tot["dirs"]), fmt_int(tot["files"]),
                       fmt_int(tot["mp3"]), fmt_int(tot["images"])))
        body = (
            "<!doctype html><html lang='%s'><head><meta charset=utf-8>"
            "<link rel='icon' type='image/jpeg' href='/dlna/icon64.jpg'>"
            "<title>web-dlna</title><style>%s</style></head><body>"
            "<h1><img src='/dlna/icon64.jpg' width='30' height='30' "
            "style='vertical-align:middle'> %s</h1>"
            "%s"
            "<table><tr><th>%s</th><th>%s</th><th>%s</th><th>%s</th>"
            "<th class=mp3>%s</th><th>%s</th><th>%s</th><th></th></tr>%s</table>"
            "<form class=add method='post' action='/admin'>"
            "<input type='hidden' name='action' value='add'>"
            "<h3>%s</h3>"
            "<label>%s: <input name='name' required placeholder='%s'></label><br>"
            "<label>%s <input name='url' size='52' required "
            "placeholder='%s'></label><br>"
            "<button class=button style='margin-top:10px' "
            "onclick=\"return confirm('%s')\">%s</button>"
            "</form>"
            "%s"
            "<p><a class=button href='/'>%s</a></p>"
            "</body></html>"
        ) % (lang, self.style(), tr["h_sources"], msg_html,
             tr["th_source"], tr["th_url"], tr["th_dirs"], tr["th_files"],
             tr["th_mp3"], tr["th_images"], tr["th_status"], "".join(rows),
             tr["new_source"], tr["name"], tr["name_ph"], tr["url"], tr["url_ph"],
             tr["confirm_add"], tr["add"], lang_select, tr["back"])
        return self.send_bytes(200, body, "text/html; charset=utf-8",
                               {"Set-Cookie": cookie} if cookie else None)

    def admin_post(self):
        fields = self.form_fields()
        tr = T[request_lang(self)]
        action = fields.get("action")
        if action == "add":
            try:
                add_source(fields.get("name"), fields.get("url"))
                self.redirect("/admin?kind=ok&msg="
                              + urllib.parse.quote(tr["msg_added"]))
            except SourceError as exc:
                text = tr.get(exc.key, exc.key)
                try:
                    text = text.format(**exc.kw)
                except (KeyError, IndexError):
                    pass
                self.redirect("/admin?kind=err&msg=" + urllib.parse.quote(text))
            return
        if action == "remove":
            sid = fields.get("sid") or ""
            if remove_source(sid):
                self.redirect("/admin?kind=ok&msg=" + urllib.parse.quote(tr["msg_removed"]))
            else:
                self.redirect("/admin?kind=err&msg=" + urllib.parse.quote(tr["msg_notfound"]))
            return
        self.redirect("/admin")

    def summary(self):
        lib = snapshot()
        stats = source_stats()
        ext = {}
        for node in lib["entries"].values():
            if node.get("t") == "f":
                e = node.get("ext") or ".mp3"
                ext[e] = ext.get(e, 0) + 1
        return self.send_bytes(200, {
            "name": DLNA_NAME,
            "sources": [
                dict(s, dirs=stats.get(s["id"], {}).get("dirs", 0),
                     files=stats.get(s["id"], {}).get("files", 0),
                     mp3=stats.get(s["id"], {}).get("mp3", 0),
                     images=stats.get(s["id"], {}).get("images", 0),
                     building=lib["building"].get(s["id"], False))
                for s in lib["sources"]
            ],
            "extensions": ext,
            "errors": lib["errors"][:50],
            "updated": lib["updated"],
            "location": _dlna_server.location if _dlna_server else None,
        })

    def event(self, path):
        service = CD_SERVICE if "ContentDirectory" in path else CM_SERVICE
        if self.command == "UNSUBSCRIBE":
            sid = self.headers.get("SID") or ""
            if _dlna_server:
                _dlna_server.subscribers.pop(sid, None)
            return self.send_bytes(200, b"", "text/plain")
        if not _dlna_server:
            return self.send_bytes(503, b"", "text/plain")
        callbacks = re.findall(r"<([^>]+)>", self.headers.get("CALLBACK") or "")
        sid = _dlna_server.subscribe(service, callbacks)
        self.send_response(200)
        self.send_header("SID", sid)
        self.send_header("TIMEOUT", "Second-1800")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def soap(self, service, kind):
        action = (self.headers.get("SOAPAction") or "").strip().strip('"').split("#")[-1]
        raw = self.read_body()
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return self.fault(401, "Invalid Action")
        body = None
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1] == "Body":
                body = el
                break
        op = body[0] if body is not None and len(body) else None
        name = op.tag.rsplit("}", 1)[-1] if op is not None else action
        args = {c.tag.rsplit("}", 1)[-1]: (c.text or "") for c in op} if op is not None else {}
        if kind == "cd":
            inner = self.cd_action(name, args)
        else:
            inner = self.cm_action(name, args)
        if inner is None:
            return self.fault(401, "Invalid Action")
        xml = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body><u:%sResponse xmlns:u=\"%s\">%s</u:%sResponse></s:Body></s:Envelope>"
        ) % (name, service, inner, name)
        return self.send_bytes(200, xml, 'text/xml; charset="utf-8"')

    def fault(self, code, desc):
        xml = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body><s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>"
            "<detail><UPnPError xmlns=\"urn:schemas-upnp-org:control-1-0\">"
            "<errorCode>%d</errorCode><errorDescription>%s</errorDescription>"
            "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
        ) % (code, xml_escape(desc))
        return self.send_bytes(500, xml, 'text/xml; charset="utf-8"')

    def cd_action(self, name, args):
        if name == "Browse":
            oid = args.get("ObjectID") or "0"
            flag = args.get("BrowseFlag") or "BrowseDirectChildren"
            start = int(args.get("StartingIndex") or 0)
            count = int(args.get("RequestedCount") or 0)
            if flag == "BrowseMetadata":
                didl = browse_metadata(oid)
                if didl is None:
                    raise ValueError("no such object")
                items, total = [didl], 1
            else:
                result = browse_children(oid, start, count)
                if result is None:
                    raise ValueError("no such object")
                items, total = result
            inner = (
                "<Result>%s</Result><NumberReturned>%d</NumberReturned>"
                "<TotalMatches>%d</TotalMatches><UpdateID>1</UpdateID>"
            ) % (xml_escape(didl_doc("".join(items))), len(items), total)
            return inner
        if name == "Search":
            start = int(args.get("StartingIndex") or 0)
            count = int(args.get("RequestedCount") or 0)
            items, total = search_tracks(args.get("SearchCriteria") or "")
            if count > 0:
                items = items[start:start + count]
            inner = (
                "<Result>%s</Result><NumberReturned>%d</NumberReturned>"
                "<TotalMatches>%d</TotalMatches><UpdateID>1</UpdateID>"
            ) % (xml_escape(didl_doc("".join(items))), len(items), total)
            return inner
        if name == "GetSortCapabilities":
            return "<SortCaps>dc:title,upnp:artist,upnp:album</SortCaps>"
        if name == "GetSearchCapabilities":
            return "<SearchCaps>dc:title,upnp:artist,upnp:album</SearchCaps>"
        if name == "GetSystemUpdateID":
            return "<Id>1</Id>"
        if name == "GetFeatureList":
            return "<FeatureList></FeatureList>"
        return None

    def cm_action(self, name, args):
        if name == "GetProtocolInfo":
            src = ";".join(
                "http-get:*:%s:%s" % (mime, ("DLNA.ORG_PN=%s;DLNA.ORG_OP=01" % pn) if pn else "")
                for mime, pn in AUDIO_EXT.values()
            )
            return "<Source>%s</Source><Sink></Sink>" % src
        if name == "GetCurrentConnectionIDs":
            return "<ConnectionIDs>0</ConnectionIDs>"
        if name == "GetCurrentConnectionInfo":
            return ("<RcsID>0</RcsID><AVTransportID>0</AVTransportID>"
                    "<ProtocolInfo></ProtocolInfo><PeerConnectionManager></PeerConnectionManager>"
                    "<PeerConnectionID>-1</PeerConnectionID><Direction>Output</Direction>"
                    "<Status>Unknown</Status>")
        return None

    def icon(self, name):
        file_path = os.path.join(WEB_DIR, name)
        try:
            with open(file_path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self.send_bytes(404, {"error": "no icon"})
        ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                 "png": "image/png", "ico": "image/vnd.microsoft.icon"}.get(
                     os.path.splitext(name)[1].lstrip(".").lower(), "application/octet-stream")
        return self.send_bytes(200, body, ctype, {"Cache-Control": "max-age=86400"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _on_stop(signum, frame):
    log("[shutdown] saving state before exit")
    try:
        save_state(force=True)
        _dl_hist_save(force=True)
    finally:
        os._exit(0)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    saved = load_sources()
    if saved:
        with _lock:
            _state["sources"] = saved
    else:
        with _lock:
            _state["sources"] = list(DEFAULT_SOURCES)
        save_sources()
        log("[sources] defaults written:", [s["name"] for s in DEFAULT_SOURCES])
    have_cache = load_state()
    global _dlna_server
    _dlna_server = Ssdp()
    _dlna_server.start()
    httpd = Server(("0.0.0.0", PORT), Handler)
    log("[http] listening on 0.0.0.0:%d" % PORT)
    log("[init] sources:", [(s["name"], s["base_url"]) for s in sources()],
        "cache_loaded=%s" % have_cache)
    if not have_cache:
        log("[init] no cached library, building now (one-shot)")
        threading.Thread(target=build_library, daemon=True).start()
    else:
        log("[init] using cached library; refresh only on request "
            "(POST /api/refresh)")
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_stop)
        except (ValueError, OSError):
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
