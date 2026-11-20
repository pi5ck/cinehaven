"""CineHaven's on-device AI: speech-to-subtitles with Whisper (faster-whisper).

Nothing is uploaded: the speech engine and model are downloaded once into
CineHaven's data folder (about 1 GB) and run on this computer's CPU.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading

from . import DATA_DIR
from .health import log

AI_DIR = os.path.join(DATA_DIR, "ai")
ENV_DIR = os.path.join(AI_DIR, "env")
MODELS = os.path.join(AI_DIR, "models")
SUBS_DIR = os.path.join(DATA_DIR, "aisubs")
MODEL = "small"                 # 4.6x real time on an 8-core laptop CPU; "base" is 12x but less accurate
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_subs_worker.py")
PACK_SIZE = "about 1 GB"


def _env_python():
    if sys.platform.startswith("win"):
        return os.path.join(ENV_DIR, "Scripts", "python.exe")
    return os.path.join(ENV_DIR, "bin", "python")


def ready():
    return os.path.exists(os.path.join(AI_DIR, "installed.ok")) and os.path.exists(_env_python())


def install(progress=None):
    """Create a private Python environment with faster-whisper. Raises on failure."""
    os.makedirs(AI_DIR, exist_ok=True)
    say = progress or (lambda t: None)
    say("Setting up the AI speech engine…")
    if not os.path.exists(_env_python()):
        r = subprocess.run([sys.executable, "-m", "venv", ENV_DIR], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("Couldn't create the AI environment: " + (r.stderr or r.stdout)[-300:])
    say("Downloading the AI speech engine (faster-whisper)…")
    r = subprocess.run([_env_python(), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                        "faster-whisper"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Couldn't install the AI speech engine: " + (r.stderr or r.stdout)[-300:])
    say("Downloading the speech model (Whisper small)…")
    env = dict(os.environ, HF_HOME=MODELS)
    r = subprocess.run([_env_python(), "-c", f"from faster_whisper import WhisperModel; WhisperModel('{MODEL}', "
                        "device='cpu', compute_type='int8')"], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError("Couldn't download the speech model: " + (r.stderr or r.stdout)[-300:])
    open(os.path.join(AI_DIR, "installed.ok"), "w").write(MODEL)
    log.info("AI subtitle pack installed")


def subs_path(media, task):
    try:
        st = os.stat(media)
        key = f"{os.path.realpath(media)}|{st.st_size}|{task}"
    except OSError:
        key = f"{media}|{task}"
    os.makedirs(SUBS_DIR, exist_ok=True)
    return os.path.join(SUBS_DIR, hashlib.sha1(key.encode()).hexdigest()[:16] + f".{task}.srt")


def _ffmpeg():
    from . import fixvideo
    t = fixvideo._static() or fixvideo._system()
    return t[0] if t else (shutil.which("ffmpeg") or "ffmpeg")


class Subtitler:
    """Runs the worker for one video; on_update(srt_path, info) is called from a thread."""

    def __init__(self, media, start, task, on_update, on_done):
        self.media, self.task = media, task
        self.out = subs_path(media, task)
        self.proc = None
        self.on_update, self.on_done = on_update, on_done
        self.start_at = start
        if os.path.exists(self.out + ".done"):          # finished on an earlier viewing
            threading.Thread(target=lambda: (self.on_update(self.out, {"cached": True}),
                                             self.on_done(self.out, True)), daemon=True).start()
            return
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        env = dict(os.environ, HF_HOME=MODELS, PYTHONIOENCODING="utf-8")
        cmd = [_env_python(), WORKER, self.media, self.out, str(self.start_at), MODEL, self.task, _ffmpeg()]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            for line in self.proc.stdout:
                try:
                    info = json.loads(line)
                except ValueError:
                    continue
                if info.get("finished"):
                    break
                self.on_update(self.out, info)
            err = self.proc.stderr.read()
            rc = self.proc.wait()
            if rc not in (0, -15, 1) or (rc == 1 and "Traceback" in err):
                log.warning("AI subtitles stopped (%s): %s", rc, err[-400:])
            self.on_done(self.out, rc == 0)
        except Exception as e:
            log.error("AI subtitles failed: %s", e)
            self.on_done(self.out, False)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
