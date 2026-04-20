# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, jsonify, send_file, Response
from pathlib import Path
from collections import Counter
from datetime import datetime
import unicodedata
import csv
import io
import os
import time

app = Flask(__name__)

# -------------------------------
# Configuration des dossiers par genre (ENGLISH, sans accents/espaces)
# -------------------------------
GENRE_FOLDERS = {
    'helbest': 'poetry',
    'roman': 'novels',
    'sano': 'theatre',              # anciennement "şano"
    'rojname': 'newspaper',
    'malper': 'site-web',
    'corpus_specifique': 'specifique-corpus',
    'dengbej': 'traditional-songs',
    'ferheng': 'dictionary'
}

# -------------------------------
# Chemins robustes
# -------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# -------------------------------
# État en mémoire
# -------------------------------
CORPUS = []
CORPUS_LAST_UPDATE = 0
WORD_CACHE = {}
FREQ_CACHE = {}

# -------------------------------
# Utilitaires texte
# -------------------------------
_PUNCT_STRIP = ".,!?;:()[]{}'\"”“’«»—–-…/\\|*#@+=~`^$%&_"

def strip_surrogates(s: str) -> str:
    """
    Supprime directement les caractères Unicode invalides (surrogates)
    U+D800..U+DFFF => évite UnicodeEncodeError: surrogates not allowed
    """
    if not isinstance(s, str):
        return s
    return "".join(ch for ch in s if not (0xD800 <= ord(ch) <= 0xDFFF))

def u_normalize(s: str) -> str:
    s = "" if s is None else str(s)
    s = strip_surrogates(s)
    return unicodedata.normalize("NFC", s).strip().lower()

def safe_read_text(file_path: Path) -> str:
    """
    Lecture robuste :
    - utf-8
    - utf-8-sig
    - fallback utf-8 avec replace (évite bytes invalides)
    """
    try:
        return strip_surrogates(file_path.read_text(encoding="utf-8"))
    except Exception:
        try:
            return strip_surrogates(file_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return strip_surrogates(file_path.read_text(encoding="utf-8", errors="replace"))

def tokenize(text: str):
    text = strip_surrogates(text or "")
    toks = []
    for tok in text.split():
        w = u_normalize(tok).strip(_PUNCT_STRIP)
        if w:
            toks.append(w)
    return toks

def html_response(html: str) -> Response:
    html_bytes = strip_surrogates(html).encode("utf-8", errors="replace")
    return Response(html_bytes, mimetype="text/html; charset=utf-8")

# -------------------------------
# Cache des stats
# -------------------------------
def refresh_stats():
    """Reconstruit WORD_CACHE et FREQ_CACHE pour 'all' et chaque genre."""
    global WORD_CACHE, FREQ_CACHE
    WORD_CACHE, FREQ_CACHE = {}, {}

    # All
    all_words = set()
    all_freqs = Counter()
    for doc in CORPUS:
        for w in tokenize(doc["text"]):
            if len(w) > 1:
                all_words.add(w)
                all_freqs[w] += 1
    WORD_CACHE["all"] = sorted(all_words)
    FREQ_CACHE["all"] = all_freqs

    # Par genre
    for genre in GENRE_FOLDERS.keys():
        words = set()
        freqs = Counter()
        for doc in (d for d in CORPUS if d["genre"] == genre):
            for w in tokenize(doc["text"]):
                if len(w) > 1:
                    words.add(w)
                    freqs[w] += 1
        WORD_CACHE[genre] = sorted(words)
        FREQ_CACHE[genre] = freqs

    print("Stats cached.")

def get_all_words(genre: str | None = None):
    key = genre or "all"
    return WORD_CACHE.get(key, [])

def get_word_frequency(genre: str | None = None):
    key = genre or "all"
    return FREQ_CACHE.get(key, Counter())

def get_genre_stats():
    stats = {g: len(get_files_by_genre(g)) for g in GENRE_FOLDERS.keys()}
    stats['total'] = len(get_corpus())
    return stats

# -------------------------------
# Chargement / rechargement du corpus
# -------------------------------
def get_corpus_last_modified() -> float:
    last_modified = 0.0
    for folder_name in GENRE_FOLDERS.values():
        folder_path = DATA_DIR / folder_name
        if folder_path.exists():
            for file_path in folder_path.glob("*.txt"):
                try:
                    m = file_path.stat().st_mtime
                    if m > last_modified:
                        last_modified = m
                except Exception as e:
                    print(f"Warning: Impossible de lire {file_path}: {e}")
    return last_modified

# ✅ IMPORTANT: en production, on évite le reload auto (cause fréquente de surcharge/503)
def needs_reload() -> bool:
    return not CORPUS  # charge 1 seule fois, puis jamais automatiquement

def load_corpus():
    global CORPUS, CORPUS_LAST_UPDATE
    print("Loading corpus...")
    corpus = []

    for genre_key, folder_name in GENRE_FOLDERS.items():
        genre_dir = DATA_DIR / folder_name
        genre_dir.mkdir(parents=True, exist_ok=True)

        txt_files = list(genre_dir.glob("*.txt"))
        print(f"   {folder_name}: {len(txt_files)} file(s)")

        for file_path in txt_files:
            try:
                text = safe_read_text(file_path)
                text = strip_surrogates(text)
                corpus.append({
                    "filename": strip_surrogates(file_path.name),
                    "text": text,
                    "genre": genre_key,
                    "folder": folder_name,
                    "full_path": str(file_path)
                })
            except Exception as e:
                print(f"Error reading {file_path}: {e}")

    CORPUS = corpus
    CORPUS_LAST_UPDATE = time.time()
    print(f"Corpus loaded: {len(CORPUS)} documents")
    refresh_stats()
    return CORPUS

def get_corpus():
    if needs_reload():
        load_corpus()
    return CORPUS

def get_files_by_genre(genre: str):
    corpus = get_corpus()
    if genre not in GENRE_FOLDERS:
        return corpus
    return [doc for doc in corpus if doc["genre"] == genre]

# ❌ On ne charge plus au démarrage global (évite multiprocess qui surcharge)
# load_corpus()

# -------------------------------
# Recherche KWIC
# -------------------------------
def kwic_search(term: str, window: int = 25, genre: str | None = None, max_results: int = 200):
    results = []
    t = u_normalize(term)
    frequency = 0
    corpus_to_search = get_files_by_genre(genre) if genre else get_corpus()

    for doc in corpus_to_search:
        text = strip_surrogates(doc.get("text", ""))
        lines = text.splitlines()

        for line_num, line in enumerate(lines, start=1):
            tokens = tokenize(line)
            for i, token in enumerate(tokens):
                if token == t:
                    frequency += 1
                    left = " ".join(tokens[max(0, i - window):i])
                    right = " ".join(tokens[i + 1:i + window + 1])
                    results.append({
                        "left": strip_surrogates(left),
                        "word": t,
                        "right": strip_surrogates(right),
                        "source": strip_surrogates(doc["filename"]),
                        "line_num": line_num,
                        "genre": doc["genre"],
                        "folder": doc["folder"]
                    })
                    # ✅ limite dure pour éviter réponses énormes (souvent cause de 503)
                    if len(results) >= max_results:
                        return results, frequency

    return results, frequency

# -------------------------------
# Autocomplete (✅ léger: utilise WORD_CACHE, pas scan du corpus)
# -------------------------------
def get_autocomplete_suggestions(prefix: str, genre: str | None = None):
    q = u_normalize(prefix)
    if not q:
        return []

    key = genre if genre else "all"
    words = get_all_words(key)

    out = []
    # recherche “contains” comme ton code original (q in w)
    for w in words:
        if q in w:
            out.append(strip_surrogates(w))
            if len(out) >= 10:
                break
    return out

# -------------------------------
# Routes Flask
# -------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        term = (request.form.get("term", "") or "").strip()
        genre = request.form.get("genre", "all")

        # ✅ évite recherche lourde si terme vide/trop court
        if len(term) < 2:
            html = render_template(
                "results.html",
                term=strip_surrogates(term),
                results=[],
                frequency=0,
                selected_genre=genre,
                now=datetime.now()
            )
            return html_response(html)

        results, freq = kwic_search(term, genre=genre if genre != "all" else None, max_results=200)

        html = render_template(
            "results.html",
            term=strip_surrogates(term),
            results=results,
            frequency=freq,
            selected_genre=genre,
            now=datetime.now()
        )
        return html_response(html)

    html = render_template("index.html")
    return html_response(html)

@app.route("/autocomplete")
def autocomplete():
    prefix = request.args.get("q", "")
    genre = request.args.get("genre", "all")
    if not prefix.strip():
        return jsonify([])

    suggestions = get_autocomplete_suggestions(prefix, genre if genre != "all" else None)
    return jsonify(suggestions)

@app.route("/stats")
def stats():
    stats_data = get_genre_stats()
    all_words = get_all_words()
    word_freq = get_word_frequency()
    total_words = sum(word_freq.values())
    unique_words = len(all_words)

    html = render_template(
        "stats.html",
        stats=stats_data,
        genres=GENRE_FOLDERS,
        total_words=total_words,
        unique_words=unique_words,
        now=datetime.now()
    )
    return html_response(html)

@app.route("/about_corpus")
def about_corpus():
    stats_data = get_genre_stats()
    html = render_template(
        "about_corpus.html",
        stats=stats_data,
        total_docs=len(get_corpus()),
        genres=GENRE_FOLDERS
    )
    return html_response(html)

@app.route("/about_me")
def about_me():
    html = render_template("about_me.html")
    return html_response(html)

@app.route("/export_words")
def export_words():
    genre = request.args.get("genre", "all")
    include_freq = request.args.get("freq", "false").lower() == "true"

    if genre != "all":
        words = get_all_words(genre)
        word_freq = get_word_frequency(genre)
        filename = f"words_{genre}.txt"
    else:
        words = get_all_words()
        word_freq = get_word_frequency()
        filename = "words_full_corpus.txt"

    if include_freq:
        sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        content = "Word\tFrequency\n" + "\n".join(f"{strip_surrogates(w)}\t{c}" for w, c in sorted_words)
    else:
        content = "\n".join(strip_surrogates(w) for w in words)

    file_stream = io.BytesIO(content.encode("utf-8", errors="replace"))
    file_stream.seek(0)
    return send_file(
        file_stream,
        as_attachment=True,
        download_name=filename,
        mimetype="text/plain; charset=utf-8"
    )

@app.route("/export_options")
def export_options():
    try:
        total_words = sum(get_word_frequency().values())
        unique_words = len(get_all_words())

        genre_stats = {}
        for genre in GENRE_FOLDERS.keys():
            uw = len(get_all_words(genre))
            tw = sum(get_word_frequency(genre).values())
            genre_stats[genre] = {'unique_words': uw, 'total_words': tw}

        html = render_template(
            "export_options.html",
            genres=GENRE_FOLDERS,
            total_words=total_words,
            unique_words=unique_words,
            genre_stats=genre_stats
        )
        return html_response(html)
    except Exception as e:
        print(f"[ERROR] export_options failed: {e}")
        return f"Error in export_options: {e}", 500

@app.route("/neologismes")
def neologismes():
    dict_words = set()
    dict_path = DATA_DIR / "dictionary"

    if dict_path.is_dir():
        for file_path in dict_path.glob("*.txt"):
            try:
                text = safe_read_text(file_path)
                for tok in text.split():
                    dict_words.add(u_normalize(tok).strip(_PUNCT_STRIP))
            except Exception as e:
                print("Error reading dictionary:", file_path.name, e)

    corpus_words = set()
    for entry in get_corpus():
        if entry.get("folder") == "dictionary" or entry.get("genre") == "ferheng":
            continue
        for tok in tokenize(entry.get("text", "")):
            corpus_words.add(tok)

    neo = sorted(list(corpus_words - dict_words))
    html = render_template("neologismes.html", neologismes=neo, count=len(neo))
    return html_response(html)

@app.route("/export_neologismes")
def export_neologismes():
    dict_words = set()
    dict_path = DATA_DIR / "dictionary"

    if dict_path.is_dir():
        for file_path in dict_path.glob("*.txt"):
            try:
                text = safe_read_text(file_path)
                for tok in text.split():
                    dict_words.add(u_normalize(tok).strip(_PUNCT_STRIP))
            except Exception as e:
                print("Error reading dictionary:", file_path.name, e)

    corpus_words = set()
    for entry in get_corpus():
        if entry.get("folder") == "dictionary" or entry.get("genre") == "ferheng":
            continue
        for tok in tokenize(entry.get("text", "")):
            corpus_words.add(tok)

    neo = sorted(list(corpus_words - dict_words))
    content = "\n".join(strip_surrogates(w) for w in neo)

    return Response(
        content.encode("utf-8", errors="replace"),
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment;filename=neologismes.txt"}
    )

@app.route("/export_stats")
def export_stats():
    word_counts = Counter()
    for entry in get_corpus():
        for tok in tokenize(entry.get("text", "")):
            word_counts[tok] += 1

    sorted_words = word_counts.most_common()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["word", "frequency"])
    for word, count in sorted_words:
        writer.writerow([strip_surrogates(word), count])

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment;filename=stats_corpus.csv"}
    )

@app.route("/reload_corpus")
def reload_corpus():
    load_corpus()
    return "Corpus reloaded!"

# -------------------------------
# Run
# -------------------------------
if __name__ == "__main__":
    app.run(debug=True, extra_files=[str(DATA_DIR)])