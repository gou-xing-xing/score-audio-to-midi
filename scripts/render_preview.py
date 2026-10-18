"""Render the saved piano MIDI and verify audible CC64 behavior per channel.

Requires an explicitly supplied SF2. No model, soundfont or network download.
The adapter is a preview approximation; it never changes the MIDI note-offs.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
import wave
from pathlib import Path

import mido
import numpy as np
import tinysoundfont

from midi_project import inspect_midi, require, sha256, write_json
from prepare_inputs import ffmpeg_exe


class SustainAdapter:
    def __init__(self, synth):
        from collections import defaultdict
        self.synth = synth
        self.pedal = defaultdict(bool)
        self.pressed = defaultdict(set)
        self.deferred = defaultdict(set)

    def flush(self, channel):
        for pitch in sorted(self.deferred[channel]):
            self.synth.noteoff(channel, pitch)
        self.deferred[channel].clear()

    def noteon(self, channel, pitch, velocity):
        if pitch in self.deferred[channel]:
            self.synth.noteoff(channel, pitch)
            self.deferred[channel].remove(pitch)
        self.pressed[channel].add(pitch)
        self.synth.noteon(channel, pitch, velocity)

    def noteoff(self, channel, pitch):
        self.pressed[channel].discard(pitch)
        if self.pedal[channel]:
            self.deferred[channel].add(pitch)
        else:
            self.synth.noteoff(channel, pitch)

    def control_change(self, channel, control, value):
        if control == 64:
            self.pedal[channel] = value >= 64
            if not self.pedal[channel]:
                self.flush(channel)
            return
        if control == 121:
            self.pedal[channel] = False
            self.flush(channel)
        elif control == 120:
            self.pressed[channel].clear()
            self.deferred[channel].clear()
        elif control == 123:
            for pitch in list(self.pressed[channel]):
                self.noteoff(channel, pitch)
            return
        self.synth.control_change(channel, control, value)


def difference(a, b):
    require(a.shape == b.shape and np.isfinite(a).all() and np.isfinite(b).all(), "Invalid A/B samples")
    denominator = float(np.sqrt(np.mean(np.square(a, dtype=np.float64))))
    numerator = float(np.sqrt(np.mean(np.square(a - b, dtype=np.float64))))
    return numerator / max(denominator, 1e-12)


def create_synth(sf2_bytes, channels, rate, bank, preset):
    synth = tinysoundfont.Synth(gain=-9, samplerate=rate)
    sfid = synth.sfload(sf2_bytes)  # bytes avoids native Unicode-path limitations
    name = synth.sfpreset_name(sfid, bank, preset)
    require(name is not None, f"No SF2 bank {bank}, preset {preset}; inspect the soundfont")
    for channel in channels:
        synth.program_select(channel, sfid, bank, preset)
    return synth, name


def samples(synth, count):
    return np.frombuffer(synth.generate(count), np.float32).reshape(-1, 2).copy()


def probe(sf2, channel, rate, bank, preset, enable_pedal):
    synth, _ = create_synth(sf2, [channel], rate, bank, preset)
    synth.noteon(channel, 60, 80)
    result = [samples(synth, round(0.1 * rate))]
    synth.control_change(channel, 64, 127 if enable_pedal else 0)
    result.append(samples(synth, round(0.15 * rate)))
    synth.noteoff(channel, 60)
    result.append(samples(synth, round(0.8 * rate)))
    synth.control_change(channel, 64, 0)
    result.append(samples(synth, round(0.3 * rate)))
    return np.concatenate(result)


def render(midi, sf2, channels, rate, bank, preset, mode, enable_pedal=True, only_channel=None, tail=1.0):
    synth, _ = create_synth(sf2, channels, rate, bank, preset)
    engine = SustainAdapter(synth) if mode == "adapter" else synth
    output, position, time = [], 0, 0.0
    for msg in mido.MidiFile(midi, charset="utf8"):
        time += msg.time
        stop = round(time * rate)
        if stop > position:
            output.append(samples(synth, stop - position))
            position = stop
        if msg.is_meta or (only_channel is not None and getattr(msg, "channel", only_channel) != only_channel):
            continue
        if msg.type == "note_on" and msg.velocity:
            engine.noteon(msg.channel, msg.note, msg.velocity)
        elif msg.type == "note_off" or (msg.type == "note_on" and not msg.velocity):
            engine.noteoff(msg.channel, msg.note)
        elif msg.type == "control_change":
            value = 0 if msg.control == 64 and not enable_pedal else msg.value
            engine.control_change(msg.channel, msg.control, value)
        elif msg.type == "pitchwheel":
            synth.pitchbend(msg.channel, msg.pitch + 8192)
        # All channels explicitly use the selected piano SF2 preset. A program
        # change in a general multi-instrument MIDI is outside this renderer.
    if mode == "adapter":
        require(not any(engine.pressed.values()) and not any(engine.deferred.values())
                and not any(engine.pedal.values()), "Adapter ended with active notes/pedal")
    output.append(samples(synth, round(tail * rate)))
    return np.concatenate(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("midi", type=Path)
    parser.add_argument("--sf2", type=Path, required=True)
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--mp3", type=Path)
    parser.add_argument("--bank", type=int, default=0)
    parser.add_argument("--preset", type=int, default=0, help="SF2 piano preset; not the exported MIDI program")
    parser.add_argument("--sample-rate", type=int, default=44100)
    parser.add_argument("--pedal-mode", choices=["auto", "native", "adapter"], default="auto")
    parser.add_argument("--tail", type=float, default=1.0)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    require(8000 <= args.sample_rate <= 96000 and 0.1 <= args.tail <= 30, "Invalid sample rate or render tail")
    report_path = args.wav.with_suffix(".render.json")
    outputs = [args.wav, report_path] + ([args.mp3] if args.mp3 else [])
    require(len({p.resolve() for p in outputs}) == len(outputs), "Output paths must be distinct")
    require(all(p.resolve() not in (args.midi.resolve(), args.sf2.resolve()) for p in outputs), "Output would replace an input")
    require(args.overwrite or not any(p.exists() for p in outputs), "Output exists; use a new revision name or --overwrite")
    source_hash = sha256(args.midi)
    audit = inspect_midi(args.midi)
    channels = sorted({n[0] for n in audit["notes"]})
    require(bool(channels) and 9 not in channels, "No piano notes, or percussion channel present")
    sf2 = args.sf2.read_bytes()
    native_effects, probe_variability = {}, {}
    for ch in channels:
        wet = probe(sf2, ch, args.sample_rate, args.bank, args.preset, True)
        dry = probe(sf2, ch, args.sample_rate, args.bank, args.preset, False)
        native_effects[ch] = difference(wet, dry)
        repeat = probe(sf2, ch, args.sample_rate, args.bank, args.preset, False)
        probe_variability[ch] = difference(dry, repeat)
    native_ok = all(effect > max(1e-4, probe_variability[ch] * 10) for ch, effect in native_effects.items())
    mode = args.pedal_mode
    if mode == "auto":
        mode = "native" if native_ok else "adapter"
    if mode == "native":
        require(native_ok or not sum(audit["captured_key_releases"].values()),
                "Selected engine/SF2 has ineffective native CC64; use auto/adapter or another soundfont")
    print(json.dumps({"pedal_mode": mode, "native_probe": native_effects}), flush=True)
    config = (args.midi, sf2, channels, args.sample_rate, args.bank, args.preset, mode)
    wet = render(*config, tail=args.tail)
    dry = render(*config, enable_pedal=False, tail=args.tail)
    mix_difference = difference(wet, dry)
    channel_effects = {}
    for ch in channels:
        if not audit["captured_key_releases"].get(ch, 0):
            channel_effects[ch] = {"status": "not_applicable_no_key_releases_under_pedal"}
            continue
        on = render(*config, only_channel=ch, tail=args.tail)
        off = render(*config, enable_pedal=False, only_channel=ch, tail=args.tail)
        effect = difference(on, off)
        require(effect > 1e-5, f"CC64 has no measurable preview effect on channel {ch}; review SF2/timing")
        channel_effects[ch] = {"status": "passed", "difference_ratio": effect}
        print(f"Channel {ch} CC64 ablation passed: {effect:.6f}", flush=True)
    require(np.isfinite(wet).all() and float(np.max(np.abs(wet))) > 1e-6, "Silent or invalid render")
    scale = 0.85 / float(np.max(np.abs(wet)))
    wet *= scale
    require(float(np.max(np.abs(wet))) < 1, "PCM render clips")
    for target in outputs:
        target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(args.wav), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(args.sample_rate)
        stream.writeframes((wet * 32767).astype("<i2").tobytes())
    mp3_peak = None
    if args.mp3:
        ffmpeg = ffmpeg_exe(args.ffmpeg)
        subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y" if args.overwrite else "-n",
                        "-i", str(args.wav), "-codec:a", "libmp3lame", "-b:a", "192k", str(args.mp3)], check=True)
        decoded = subprocess.check_output([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(args.mp3),
                                           "-f", "f32le", "-acodec", "pcm_f32le", "-"])
        pcm = np.frombuffer(decoded, "<f4")
        require(pcm.size > 0 and np.isfinite(pcm).all(), "MP3 did not decode cleanly")
        mp3_peak = float(np.max(np.abs(pcm)))
        require(mp3_peak < 1.0, "Decoded MP3 clips; reduce render gain")
        require(abs(pcm.size / 2 / args.sample_rate - len(wet) / args.sample_rate) < 0.2, "MP3 duration mismatch")
    require(sha256(args.midi) == source_hash, "Source MIDI changed during rendering")
    _, preset_name = create_synth(sf2, channels, args.sample_rate, args.bank, args.preset)
    report = {"midi": str(args.midi.resolve()), "midi_sha256": source_hash,
              "sf2": str(args.sf2.resolve()), "sf2_sha256": sha256(args.sf2),
              "bank": args.bank, "preset": args.preset, "preset_name": preset_name,
              "engine": "tinysoundfont", "engine_version": importlib.metadata.version("tinysoundfont"),
              "pedal_mode": mode, "native_probe_difference": native_effects,
              "native_probe_variability": probe_variability,
              "full_mix_pedal_difference": mix_difference, "channel_pedal_checks": channel_effects,
              "sample_rate": args.sample_rate, "duration_seconds": len(wet) / args.sample_rate,
              "gain_scale": scale, "wav_peak": float(np.max(np.abs(wet))), "decoded_mp3_peak": mp3_peak,
              "midi_unmodified": True, "wav": str(args.wav.resolve()),
              "mp3": str(args.mp3.resolve()) if args.mp3 else None,
              "limitations": "Sample-based preview; no sympathetic resonance, repedalling or half-pedal model in adapter mode."}
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
