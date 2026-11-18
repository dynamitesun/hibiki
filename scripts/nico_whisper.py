#!/usr/bin/env python3
"""Make timed Japanese subtitles for Niconico videos with Whisper (speech-to-text),
split them into words, and save them in data/cap/<id>.json for the app.
Runs after build_captions.py, which writes data/nico.json."""
import glob, json, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_captions as bc

MINUTES = float(os.environ.get("MINUTES", "35"))
MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
TMP = os.path.join(bc.ROOT, ".tmp_audio")
LOG = []


def download(vid):
    os.makedirs(TMP, exist_ok=True)
    for f in glob.glob(os.path.join(TMP, "*")):
        os.remove(f)
    r = subprocess.run(["yt-dlp", "-q", "--no-warnings", "-f", "bestaudio/worst", "-o", os.path.join(TMP, vid + ".%(ext)s"),
                        "https://www.nicovideo.jp/watch/" + vid], capture_output=True, text=True, timeout=240)
    files = glob.glob(os.path.join(TMP, vid + ".*"))
    if not files:
        raise RuntimeError((r.stderr or "no file").strip().splitlines()[-1][:160])
    return files[0]


def audio16k(path):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True, timeout=300).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def clean(segs):
    out, last = [], ""
    for s in segs:
        t = s.text.strip().replace(" ", "")
        if not t or not bc.JP.search(t):
            continue
        if s.no_speech_prob > 0.6 or s.avg_logprob < -1.0 or s.compression_ratio > 2.4:
            continue  # music / noise / made-up repeats
        if t == last:
            continue
        last = t
        out.append([round(s.start, 2), round(s.end, 2), t])
    return out


def main():
    from faster_whisper import WhisperModel
    t0 = time.time()
    nico = json.load(open(os.path.join(bc.DATA, "nico.json"), encoding="utf-8"))
    idx_path = os.path.join(bc.DATA, "index.json")
    index = json.load(open(idx_path)) if os.path.exists(idx_path) else {}
    def need(v):
        e = index.get(v["id"], {})
        if v["len"] > 900 or e.get("tries", 0) >= 2:
            return False
        return not e.get("n") or (e.get("w") and e.get("m") != MODEL)  # none yet, or made with an older model
    todo = sorted([v for v in nico if need(v)],
                  key=lambda v: (bool(index.get(v["id"], {}).get("n")), not v["short"], v["len"]))
    LOG.append(f"whisper: {len(todo)} videos waiting")
    bc.load_dict()
    subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)  # stop early if it's missing
    model = WhisperModel(MODEL, device="cpu", compute_type="int8")
    done = 0
    for v in todo:
        if time.time() - t0 > MINUTES * 60:
            break
        vid = v["id"]
        err = ""
        t1 = time.time()
        try:
            path = download(vid)
            segs, _ = model.transcribe(audio16k(path), language="ja", vad_filter=True, beam_size=2,
                                       condition_on_previous_text=False, initial_prompt="こんにちは。今日は、")
            cues = clean(list(segs))
        except Exception as e:
            LOG.append(f"  {vid} failed: {e}")
            cues, err = [], str(e)
        if not cues and index.get(vid, {}).get("n"):
            continue  # keep the older subtitles
        rec = {"title": v["t"], "tw": bc.words(v["t"]), "cues": [[s, e, bc.words(line)] for s, e, line in cues], "src": "whisper"}
        json.dump(rec, open(os.path.join(bc.CAP, vid + ".json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        old = index.get(vid, {})
        index[vid] = {"lv": v["lv"], "short": v["short"], "n": len(cues), "tries": old.get("tries", 0) + (0 if cues or "ffmpeg" in err or "metadata_errors" in err else 1), "g": bool(bc.DICT), "w": 1, "m": MODEL}
        done += bool(cues)
        print(f"  {vid}: {len(cues)} lines in {int(time.time() - t1)}s  {v['t'][:40]}", flush=True)
        json.dump(index, open(idx_path, "w"), separators=(",", ":"))
    LOG.append(f"whisper: {done} videos got subtitles in {int(time.time() - t0)}s")
    with open(os.path.join(bc.DATA, "debug.txt"), "a") as f:
        f.write("\n".join(LOG) + "\n")


if __name__ == "__main__":
    main()
