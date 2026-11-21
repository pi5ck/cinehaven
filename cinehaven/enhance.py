"""Picture and sound enhancement for the player.

Picture – "Enhance picture":
  * AI upscaling: FSRCNNX, a small neural network run as a GPU shader, doubles the
    detail of videos smaller than the window (DVD-quality and old films gain most).
  * Sharp scaling (spline36), debanding of smooth gradients, and a gentle
    denoise for grainy standard-definition sources.
Sound:
  * Dialogue boost: lifts the speech frequencies and, for 5.1 films, the centre
    channel where voices live; a light compressor keeps speech clear over music.
  * Night mode: evens out loudness so explosions don't wake the house and whispers
    stay audible.
"""
import os

SHADER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shaders", "FSRCNNX_x2_8-0-4-1.glsl")

DIALOGUE_STEREO = ("lavfi=[equalizer=f=180:t=q:w=1:g=-3,equalizer=f=2600:t=q:w=1.3:g=5,"
                   "acompressor=threshold=-22dB:ratio=3:attack=8:release=250:makeup=3]")
DIALOGUE_SURROUND = ("lavfi=[pan=stereo|FL<0.5*FL+1.3*FC+0.35*BL+0.35*SL|FR<0.5*FR+1.3*FC+0.35*BR+0.35*SR,"
                     "equalizer=f=2600:t=q:w=1.3:g=3,"
                     "acompressor=threshold=-22dB:ratio=3:attack=8:release=250:makeup=3]")
NIGHT = "lavfi=[dynaudnorm=f=200:g=15:p=0.8:m=12,alimiter=limit=0.9]"


def audio_filters(dialogue, night, channels=2):
    chain = []
    if dialogue:
        chain.append(DIALOGUE_SURROUND if channels and channels >= 6 else DIALOGUE_STEREO)
    if night:
        chain.append(NIGHT)
    return ",".join(chain)


def apply_audio(m, dialogue, night):
    try:
        ch = (m.audio_params or {}).get("channel-count") or 2
    except Exception:
        ch = 2
    try:
        m.af = audio_filters(dialogue, night, ch)
    except Exception:
        m.af = ""


def _restore_defaults(m):
    """Put scaling options back to mpv's own defaults (as this mpv version reports them)."""
    for k in ("scale", "cscale", "deband"):
        try:
            m.command("set", k, m._get_property(f"option-info/{k}/default-value"))
        except Exception:
            pass


def apply_picture(m, on, out_height=None, level=2):
    """Turn picture enhancement on/off for the current video.

    level 1 = sharp scaling + deband; 2 = also AI upscaling and denoise of small videos;
    3 = also EWA Lanczos scaling (sharpest, heaviest). adapt.py learns which level each kind
    of video can afford on this computer."""
    try:
        vp = m.video_params or {}
    except Exception:
        vp = {}
    h = vp.get("h") or 0
    if not on:
        m.glsl_shaders = []
        m.vf = ""
        _restore_defaults(m)
        return
    m.scale = "ewa_lanczossharp" if level >= 3 else "spline36"
    m.cscale = "spline36"
    m.deband = True
    upscale = level >= 2 and h and (out_height or 1080) >= h * 1.3 and h <= 1080 and os.path.exists(SHADER)
    m.glsl_shaders = [SHADER] if upscale else []
    m.vf = "lavfi=[hqdn3d=1.5:1.5:4:4]" if level >= 2 and h and h <= 576 else ""


def describe(m):
    """Short status line for the menu: what enhancement is doing right now."""
    try:
        vp = m.video_params or {}
        shaders = m.glsl_shaders or []
    except Exception:
        return ""
    bits = []
    try:
        if m.scale == "ewa_lanczossharp":
            bits.append("EWA scaling")
    except Exception:
        pass
    if shaders:
        bits.append(f"AI upscaling {vp.get('w')}×{vp.get('h')}")
    if m.vf:
        bits.append("denoise")
    if m.deband:
        bits.append("deband")
    return " · ".join(bits)
