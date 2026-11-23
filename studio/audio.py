"""Audio engine: background music, auto-ducking, voice enhancement, and sound effects."""
import json
import math
import os
from pathlib import Path
import struct
import wave

from . import config

MUSIC_DIR = config.DATA_DIR / "music"
SFX_DIR = config.DATA_DIR / "sfx"


def _generate_wav(path: Path, duration_sec: float, sample_rate: int, generator_fn):
    """Generate a clean 16-bit mono PCM WAV file using a sample-generator function."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = int(duration_sec * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        raw = bytearray()
        for i in range(n_samples):
            t = i / sample_rate
            val = generator_fn(t, duration_sec)
            val = max(-1.0, min(1.0, val))
            sample_val = int(val * 32767.0)
            raw.extend(struct.pack("<h", sample_val))
        wf.writeframes(raw)


def ensure_audio_assets():
    """Create starter ambient music tracks and sound effects if they don't exist yet."""
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    SFX_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Ambient Chill Synth Pad (12s seamless chord loop: Cmaj7 - Am7 - Fmaj7 - G7)
    synth_path = MUSIC_DIR / "chill_synth.wav"
    if not synth_path.exists():
        def _synth_fn(t, dur):
            # Smooth chord pad: 130.81 (C3), 164.81 (E3), 196.00 (G3), 246.94 (B3)
            # Cycle every 3s
            chord_idx = int(t / 3.0) % 4
            freqs = [
                [130.81, 164.81, 196.00, 246.94],  # Cmaj7
                [110.00, 130.81, 164.81, 196.00],  # Am7
                [87.31, 130.81, 174.61, 220.00],   # Fmaj7
                [98.00, 123.47, 146.83, 196.00],   # G7
            ][chord_idx]
            sig = 0.0
            for f in freqs:
                sig += 0.22 * math.sin(2 * math.pi * f * t)
                sig += 0.08 * math.sin(2 * math.pi * (f * 2) * t)  # soft harmonic
            # Envelope per chord
            local_t = t % 3.0
            env = math.sin(math.pi * (local_t / 3.0))
            return sig * env * 0.7

        _generate_wav(synth_path, 12.0, 44100, _synth_fn)

    # 2. Lo-Fi Chill Groove (8s loop)
    lofi_path = MUSIC_DIR / "lofi_vibe.wav"
    if not lofi_path.exists():
        def _lofi_fn(t, dur):
            # Soft warm electric piano chord + subtle beat
            f1, f2 = 146.83, 220.00  # D3 + A3
            tone = 0.25 * math.sin(2 * math.pi * f1 * t) + 0.20 * math.sin(2 * math.pi * f2 * t)
            # Subtle rhythmic low pulse every 1s
            pulse = math.exp(-12.0 * (t % 1.0)) * 0.35 * math.sin(2 * math.pi * 55.0 * t)
            return (tone + pulse) * 0.65

        _generate_wav(lofi_path, 8.0, 44100, _lofi_fn)

    # 3. SFX: Whoosh (0.4s fast transition sweep)
    whoosh_path = SFX_DIR / "whoosh.wav"
    if not whoosh_path.exists():
        def _whoosh_fn(t, dur):
            freq = 200.0 + 800.0 * (t / dur)
            env = math.sin(math.pi * (t / dur))
            # White noise blend
            noise = (math.sin(2 * math.pi * 1337.0 * t * t) % 1.0 - 0.5) * 0.4
            return (math.sin(2 * math.pi * freq * t) * 0.6 + noise) * env

        _generate_wav(whoosh_path, 0.4, 44100, _whoosh_fn)

    # 4. SFX: Pop (0.15s UI bubble pop)
    pop_path = SFX_DIR / "pop.wav"
    if not pop_path.exists():
        def _pop_fn(t, dur):
            freq = 700.0 - 400.0 * (t / dur)
            env = math.exp(-18.0 * (t / dur))
            return math.sin(2 * math.pi * freq * t) * env

        _generate_wav(pop_path, 0.15, 44100, _pop_fn)

    # 5. SFX: Boom (0.8s cinematic bass impact)
    boom_path = SFX_DIR / "boom.wav"
    if not boom_path.exists():
        def _boom_fn(t, dur):
            freq = 110.0 * math.exp(-4.0 * (t / dur))
            env = math.exp(-3.5 * (t / dur))
            return math.sin(2 * math.pi * freq * t) * env * 0.9

        _generate_wav(boom_path, 0.8, 44100, _boom_fn)

    # 6. SFX: Censor bleep (0.5s 1000Hz test tone)
    censor_path = SFX_DIR / "censor.wav"
    if not censor_path.exists():
        def _censor_fn(t, dur):
            return math.sin(2 * math.pi * 1000.0 * t) * 0.7

        _generate_wav(censor_path, 0.5, 44100, _censor_fn)


def list_music() -> list:
    ensure_audio_assets()
    tracks = []
    for f in sorted(MUSIC_DIR.glob("*")):
        if f.suffix.lower() in (".wav", ".mp3", ".m4a", ".ogg", ".aac"):
            name = f.stem.replace("_", " ").title()
            tracks.append({
                "id": f.name,
                "name": name,
                "filename": f.name,
                "url": f"/media/music/{f.name}",
            })
    return tracks


def list_sfx() -> list:
    ensure_audio_assets()
    effects = []
    for f in sorted(SFX_DIR.glob("*")):
        if f.suffix.lower() in (".wav", ".mp3"):
            name = f.stem.replace("_", " ").title()
            effects.append({
                "id": f.stem,
                "name": name,
                "filename": f.name,
                "url": f"/media/sfx/{f.name}",
            })
    return effects


def build_audio_filtergraph(has_music: bool, music_filename: str = None, music_vol: float = 0.25,
                            ducking: bool = True, sfx_list: list = None, studio_voice: bool = False,
                            voice_input: str = "[0:a]", music_input: str = "[1:a]",
                            sfx_input_start_idx: int = 2) -> tuple:
    """Builds ffmpeg audio filter complex snippet and returns (filter_str, out_label).

    Features:
    - Studio Voice enhancement: highpass=f=80, acompressor, loudnorm
    - Background music looping with volume adjustment
    - Auto-ducking using sidechaincompress or dynamic volume ducking
    - SFX insertion with adelay and mixing
    """
    filters = []
    cur_voice = "[vocal]"

    # 1. Voice processing: Studio Voice or pass-through
    if studio_voice:
        filters.append(
            f"{voice_input}highpass=f=80,acompressor=threshold=-20dB:ratio=4:attack=5:release=50[vocal_proc];"
            f"[vocal_proc]loudnorm=I=-14:TP=-1.5:LRA=11{cur_voice}"
        )
    else:
        filters.append(f"{voice_input}anull{cur_voice}")

    # 2. SFX track mixing if any SFX are provided
    sfx_filters = []
    mixed_voice = cur_voice
    if sfx_list:
        cur_inputs = [cur_voice]
        for idx, sfx_item in enumerate(sfx_list):
            sfx_in = f"[{sfx_input_start_idx + idx}:a]"
            delay_ms = int(max(0.0, float(sfx_item.get("time", sfx_item.get("offset", 0.0)))) * 1000)
            delayed_label = f"[sfx_{idx}]"
            sfx_filters.append(f"{sfx_in}adelay={delay_ms}|{delay_ms}{delayed_label}")
            cur_inputs.append(delayed_label)
        in_str = "".join(cur_inputs)
        sfx_filters.append(f"{in_str}amix=inputs={len(cur_inputs)}:dropout_transition=2[vocal_with_sfx]")
        mixed_voice = "[vocal_with_sfx]"
        filters.extend(sfx_filters)

    # 3. Background Music & Auto-Ducking
    if has_music and music_filename:
        vol = max(0.01, min(1.0, float(music_vol)))
        # Loop music and apply base volume
        filters.append(f"{music_input}aloop=loop=-1:size=2e+09,volume={vol:.2f}[bg_music]")

        if ducking:
            # Auto-ducking via sidechaincompress: voice compresses music when voice is active
            filters.append(
                f"[bg_music]{mixed_voice}sidechaincompress=threshold=0.08:ratio=4:attack=20:release=350:makeup=1[ducked_music];"
                f"{mixed_voice}[ducked_music]amix=inputs=2:duration=first:dropout_transition=2[final_a]"
            )
        else:
            filters.append(f"{mixed_voice}[bg_music]amix=inputs=2:duration=first:dropout_transition=2[final_a]")
        return ";".join(filters), "[final_a]"

    return ";".join(filters), mixed_voice
