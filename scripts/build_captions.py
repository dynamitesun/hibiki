#!/usr/bin/env python3
"""Fetch Japanese subtitles for the newest videos of every channel in index.html,
split them into words (reading + dictionary form + English meaning), and write
small JSON files the app loads when you tap "Save word".

Output:
  data/index.json        {video_id: {"lv": [levels], "short": bool, "n": cue_count}}
  data/cap/<id>.json      {"title": str, "tw": [word...], "cues": [[start, end, [word...]], ...]}
  word = [surface, reading, base_form, gloss, pos]   (pos: n v a adv x ...)
"""
import glob, io, json, os, re, subprocess, sys, time, urllib.request, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CAP = os.path.join(DATA, "cap")
TMP = os.path.join(ROOT, ".tmp_subs")
BUDGET = int(os.environ.get("BUDGET", "150"))          # subtitle downloads per run
PER_SHORTS = int(os.environ.get("PER_SHORTS", "10"))   # newest shorts per channel
PER_LONG = int(os.environ.get("PER_LONG", "4"))        # newest long videos per channel


# ---------------------------------------------------------------- channels
def channels():
    html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    block = html[html.index("const CH=["): html.index("\n];", html.index("const CH=["))]
    out = []
    for m in re.finditer(r"\{name:(\"[^\"]*\"|'[^']*'),id:'(UC[\w-]{22})',lv:\[([\d,]+)\](,shortsOnly:true)?\}", block):
        out.append({"name": json.loads(m.group(1)) if m.group(1)[0] == '"' else m.group(1)[1:-1],
                    "id": m.group(2), "lv": [int(x) for x in m.group(3).split(",")], "shortsOnly": bool(m.group(4))})
    return out


def list_playlist(pid, n):
    try:
        r = subprocess.run(["yt-dlp", "--flat-playlist", "-J", "--playlist-end", str(n), "--no-warnings",
                            f"https://www.youtube.com/playlist?list={pid}"], capture_output=True, text=True, timeout=120)
        j = json.loads(r.stdout or "{}")
        return [(e["id"], e.get("title") or "") for e in j.get("entries") or [] if e.get("id")]
    except Exception as e:
        print("  list failed", pid, e)
        return []


# ---------------------------------------------------------------- subtitles
TS = re.compile(r"(\d+):(\d\d):(\d\d)[.,](\d+)\s*-->\s*(\d+):(\d\d):(\d\d)[.,](\d+)")


def to_s(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms[:3].ljust(3, "0")) / 1000


def parse_vtt(text):
    """Return [[start, end, line]] with YouTube's rolling auto-caption duplicates removed."""
    cues, last = [], ""
    for block in re.split(r"\n\n+", text.replace("\r", "")):
        lines = block.strip().split("\n")
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        m = TS.search(lines[idx])
        if not m:
            continue
        body = [re.sub(r"<[^>]+>", "", l).strip() for l in lines[idx + 1:]]
        body = [l for l in body if l]
        if not body:
            continue
        line = body[-1]  # rolling captions repeat the previous line above the new one
        line = re.sub(r"\[[^\]]*\]|［[^］]*］|\([^)]*音楽[^)]*\)", "", line).strip()
        if not line or line == last:
            continue
        s, e = to_s(*m.groups()[:4]), to_s(*m.groups()[4:])
        if e - s < 0.05:
            continue
        cues.append([round(s, 2), round(e, 2), line])
        last = line
    return cues


CLIENTS = ["default", "tv", "web_embedded", "mweb", "android_vr", "transcript_api"]
WORKING = []  # the method that worked last, tried first


def _ytdlp(vid, client):
    os.makedirs(TMP, exist_ok=True)
    for f in glob.glob(os.path.join(TMP, vid + "*")):
        os.remove(f)
    cmd = ["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "ja,ja-JP,ja-orig",
           "--sub-format", "vtt", "--no-warnings", "--sleep-requests", "1",
           "-o", os.path.join(TMP, "%(id)s.%(ext)s"), f"https://www.youtube.com/watch?v={vid}"]
    if client != "default":
        cmd[1:1] = ["--extractor-args", f"youtube:player_client={client}"]
    if os.path.exists(COOKIES):
        cmd[1:1] = ["--cookies", COOKIES]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    files = sorted(glob.glob(os.path.join(TMP, vid + "*.vtt")), key=lambda p: ("orig" in p, len(p)))
    if not files:
        return None, (r.stderr or r.stdout)[-300:]
    return parse_vtt(open(files[0], encoding="utf-8").read()), ""


def _transcript_api(vid):
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        api = YouTubeTranscriptApi()
        t = api.fetch(vid, languages=["ja", "ja-JP"])
        cues, last = [], ""
        for sn in t:
            line = re.sub(r"\[[^\]]*\]", "", sn.text.replace("\n", " ")).strip()
            if line and line != last:
                cues.append([round(sn.start, 2), round(sn.start + sn.duration, 2), line]); last = line
        return cues, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:250]}"


BLOCKED = {"fails": 0}
COOKIES = os.path.join(ROOT, ".tmp_cookies.txt")


def fetch_subs(vid):
    if BLOCKED["fails"] >= 3 and not WORKING:
        return []  # YouTube is blocking this machine; don't waste the run
    order = WORKING + [c for c in CLIENTS if c not in WORKING]
    for client in order:
        cues, err = _transcript_api(vid) if client == "transcript_api" else _ytdlp(vid, client)
        if cues is not None:
            if client not in WORKING[:1]:
                WORKING[:] = [client]
                DEBUG.append(f"{vid}: worked with {client}")
            return cues
        if len(DEBUG) < 40:
            DEBUG.append(f"{vid} [{client}] {err.strip()}")
        if WORKING:  # a method worked before; this video probably just has no Japanese subtitles
            break
    if not WORKING:
        BLOCKED["fails"] += 1
        if BLOCKED["fails"] == 3:
            DEBUG.append("YouTube is blocking subtitle downloads from this machine; skipping subtitles this run. Add a YT_COOKIES secret to fix.")
    return []


# ---------------------------------------------------------------- words
KATA = re.compile(r"[ァ-ヶ]")
JP = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u4e00-\u9fff々ー]")
to_hira = lambda s: KATA.sub(lambda m: chr(ord(m.group()) - 0x60), s or "")
POS = {"名詞": "n", "動詞": "v", "形容詞": "a", "副詞": "adv", "助詞": "p", "助動詞": "aux", "連体詞": "adn",
       "接続詞": "conj", "感動詞": "int", "記号": "x", "接頭詞": "pre", "フィラー": "fil"}
_tok = None
DICT = {}
DEBUG = []


def load_dict():
    """JMdict (common words, English) from the jmdict-simplified project."""
    try:
        req = urllib.request.Request("https://api.github.com/repos/scriptin/jmdict-simplified/releases/latest",
                                     headers={"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"]} if os.environ.get("GITHUB_TOKEN") else {})
        api = json.load(urllib.request.urlopen(req, timeout=60))
        url = next(a["browser_download_url"] for a in api["assets"] if re.match(r"jmdict-eng-common-.*\.json\.zip$", a["name"]))
        z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(url, timeout=120).read()))
        j = json.loads(z.read(z.namelist()[0]))
        for w in j["words"]:
            gl = "; ".join(g["text"] for g in w["sense"][0]["gloss"][:3])
            for k in w.get("kanji", []):
                DICT.setdefault(k["text"], gl)
            for k in w.get("kana", []):
                DICT.setdefault(k["text"], gl)
        print("dictionary entries:", len(DICT))
    except Exception as e:
        print("dictionary unavailable:", e)
        DEBUG.append(f"dictionary unavailable: {e}")


def words(text):
    global _tok
    if _tok is None:
        from janome.tokenizer import Tokenizer
        _tok = Tokenizer()
    out = []
    for t in _tok.tokenize(text):
        pos0, pos1 = t.part_of_speech.split(",")[:2]
        surf, base = t.surface, (t.base_form if t.base_form != "*" else t.surface)
        rd = to_hira(t.reading if t.reading != "*" else surf)
        prev = out[-1] if out else None
        attach = prev and prev[4] in ("v", "a") and (pos0 == "助動詞" or (pos0 == "動詞" and pos1 in ("非自立", "接尾"))
                                                      or (pos0 == "助詞" and pos1 == "接続助詞" and surf in ("て", "で")))
        if attach:
            prev[0] += surf
            prev[1] += rd
            continue
        p = POS.get(pos0, "o")
        if not JP.search(surf):
            p = "x"  # English words, numbers and symbols aren't offered as Japanese words
        if pos0 == "名詞" and pos1 == "形容動詞語幹":
            p = "na"
        if pos0 == "名詞" and pos1 in ("数", "接尾", "非自立", "代名詞") and p == "n":
            p = "n2"
        out.append([surf, rd, base, DICT.get(base) or DICT.get(surf) or "", p])
    return out


# ---------------------------------------------------------------- main
def main():
    os.makedirs(CAP, exist_ok=True)
    if os.environ.get("YT_COOKIES", "").strip():
        open(COOKIES, "w").write(os.environ["YT_COOKIES"])
        DEBUG.append("using YouTube cookies")
    idx_path = os.path.join(DATA, "index.json")
    index = json.load(open(idx_path)) if os.path.exists(idx_path) else {}
    load_dict()
    queues = []
    for ch in channels():
        todo = []
        lists = [("UUSH" + ch["id"][2:], PER_SHORTS, True)] + ([] if ch["shortsOnly"] else [("UULF" + ch["id"][2:], PER_LONG, False)])
        for pid, n, short in lists:
            for vid, title in list_playlist(pid, n):
                if vid in index:
                    index[vid]["lv"] = ch["lv"]
                    if index[vid].get("n") or (index[vid].get("tries", 0) >= 3 and index[vid].get("g")):
                        continue
                todo.append((vid, title, ch["lv"], short))
        queues.append(todo)
        print(f"{ch['name']}: {len(todo)} new")
    # round-robin across channels so every channel and level gets coverage within the budget
    todo = [q[i] for i in range(max(map(len, queues or [[]]))) for q in queues if i < len(q)]
    done = 0
    for vid, title, lv, short in todo:
        if done >= BUDGET:
            break
        done += 1
        try:
            cues = fetch_subs(vid)
        except Exception as e:
            print("  subs failed", vid, e)
            cues = []
        rec = {"title": title, "tw": words(title) if title else [],
               "cues": [[s, e, words(line)] for s, e, line in cues]}
        json.dump(rec, open(os.path.join(CAP, vid + ".json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        index[vid] = {"lv": lv, "short": short, "n": len(cues), "tries": index.get(vid, {}).get("tries", 0) + (0 if cues else 1), "g": bool(DICT)}
        print(f"  {vid}: {len(cues)} lines  {title[:40]}")
        time.sleep(0.5)
    json.dump(index, open(idx_path, "w"), separators=(",", ":"))
    open(os.path.join(DATA, "debug.txt"), "w").write("\n".join(DEBUG) + f"\nworking method: {WORKING}\n")
    print(f"processed {done}, index size {len(index)}, with subtitles {sum(1 for v in index.values() if v['n'])}")


if __name__ == "__main__":
    main()
