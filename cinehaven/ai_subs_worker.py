"""AI subtitle worker – runs inside CineHaven's private AI environment (faster-whisper).

Usage: python ai_subs_worker.py MEDIA OUT.srt START_SECONDS MODEL TASK FFMPEG
  TASK = "transcribe" (subtitles in the spoken language) or "translate" (English subtitles)

Works forward from START in one-minute chunks, so subtitles appear while you watch.
After each chunk OUT.srt is rewritten (existing lines kept) and a JSON progress line is
printed: {"done_to": seconds, "lang": "en", "lines": n}.
"""
import json
import os
import re
import subprocess
import sys

import numpy as np
from faster_whisper import WhisperModel

CHUNK = 30               # small pieces keep the AI ahead of playback
FIRST_CHUNK = 15         # short first piece, so the first subtitles appear within seconds
RATE = 16000


def ts(t):
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"


def parse_srt(path):
    cues = {}
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return cues
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        if len(lines) >= 3:
            m = re.match(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", lines[1])
            if m:
                g = [int(x) for x in m.groups()]
                start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                cues[round(start, 2)] = (start, end, "\n".join(lines[2:]))
    return cues


def write_srt(path, cues):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(sorted(cues.values()), 1):
            f.write(f"{i}\n{ts(start)} --> {ts(end)}\n{text}\n\n")
    os.replace(tmp, path)


def duration(ffmpeg, media):
    probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + (".exe" if ffmpeg.endswith(".exe") else ""))
    try:
        out = subprocess.run([probe if os.path.exists(probe) else "ffprobe", "-v", "error", "-show_entries",
                              "format=duration", "-of", "csv=p=0", media], capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip())
    except Exception:
        return None


def main():
    media, out, start, model_name, task, ffmpeg = sys.argv[1:7]
    pos = max(float(start) - 2, 0)
    total = duration(ffmpeg, media)
    threads = max(2, (os.cpu_count() or 4) - 2)
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
    cues = parse_srt(out)
    lang = None
    first = True
    while total is None or pos < total:
        length = FIRST_CHUNK if first else CHUNK
        first = False
        pcm = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-ss", str(pos), "-t", str(length), "-i", media,
                              "-vn", "-f", "f32le", "-ac", "1", "-ar", str(RATE), "-"],
                             capture_output=True).stdout
        audio = np.frombuffer(pcm, np.float32)
        if audio.size < RATE:              # end of the file
            break
        segs, info = model.transcribe(audio, task=task, language=lang, vad_filter=True, beam_size=1,
                                      condition_on_previous_text=False)
        lang = lang or info.language
        for s in segs:
            text = s.text.strip()
            if text:
                cues[round(pos + s.start, 2)] = (pos + s.start, pos + s.end, text)
        write_srt(out, cues)
        pos += audio.size / RATE
        print(json.dumps({"done_to": round(pos, 1), "lang": lang, "lines": len(cues), "total": total}), flush=True)
    if float(start) <= 3:                  # whole film covered: mark it so it's reused next time
        open(out + ".done", "w").close()
    print(json.dumps({"finished": True, "lines": len(cues)}), flush=True)


if __name__ == "__main__":
    main()
