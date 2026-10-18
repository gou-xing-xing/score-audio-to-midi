"""Export and independently read back a reviewed score/audio piano project.

Only mido is required. This module does not infer notes or alter key lengths.
See references/project-format.md for the JSON contract.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import hashlib
import json
import math
from pathlib import Path

import mido


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def number(value, name):
    require(isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value), f"{name} must be a finite number")
    return float(value)


def integer(value, low, high, name):
    require(type(value) is int and low <= value <= high, f"{name} must be an integer in {low}..{high}")


class TempoMap:
    def __init__(self, segments, origin=0.0, tpq=1920):
        require(bool(segments), "tempo_map is empty")
        integer(tpq, 24, 32767, "ticks_per_beat")
        self.tpq = tpq
        self.origin = number(origin, "origin_seconds")
        self.beats, self.tempos, self.seconds = [], [], []
        for entry in segments:
            beat = number(entry["beat"], "tempo beat")
            tempo = entry["us_per_beat"]
            integer(tempo, 1, 0xFFFFFF, "tempo us_per_beat")
            require(beat >= 0 and abs(beat * tpq - round(beat * tpq)) < 1e-7,
                    "tempo beat must be nonnegative and representable at the chosen TPQ")
            if self.beats:
                require(beat > self.beats[-1], "tempo beats must strictly increase")
                second = self.seconds[-1] + (beat - self.beats[-1]) * self.tempos[-1] / 1e6
            else:
                require(beat == 0, "tempo_map must start at beat 0")
                second = 0.0
            self.beats.append(beat)
            self.tempos.append(tempo)
            self.seconds.append(second)

    def tick(self, audio_seconds):
        second = number(audio_seconds, "event time") - self.origin
        require(second >= -1e-9, "event precedes origin_seconds; do not silently clip it")
        index = max(0, bisect.bisect_right(self.seconds, second) - 1)
        beat = self.beats[index] + (second - self.seconds[index]) * 1e6 / self.tempos[index]
        return round(beat * self.tpq)

    def audio_seconds(self, tick):
        beat = tick / self.tpq
        index = max(0, bisect.bisect_right(self.beats, beat) - 1)
        return self.origin + self.seconds[index] + (beat - self.beats[index]) * self.tempos[index] / 1e6


def validate_project(project):
    require(project.get("schema_version") == 1, "schema_version must be 1")
    clock = TempoMap(project["tempo_map"], project.get("origin_seconds", 0),
                     project.get("ticks_per_beat", 1920))
    channels = {}
    for channel in project["channels"]:
        ch = channel["channel"]
        integer(ch, 0, 15, "channel")
        require(ch != 9, "Piano must not use GM percussion channel 9 (zero-based)")
        require(ch not in channels, f"duplicate channel {ch}")
        integer(channel.get("program", 0), 0, 127, "program")
        channels[ch] = channel
    require(bool(channels), "channels is empty")
    bars = {str(b["id"]): b for b in project["bars"]}
    require(len(bars) == len(project["bars"]) and bool(bars), "bars must have unique IDs")
    for b in bars.values():
        require(number(b["beat"], "bar beat") >= 0, "bar beat must be nonnegative")
    reviewed = list(map(str, project["reviewed_bars"]))
    require(len(reviewed) == len(set(reviewed)) and set(reviewed) == set(bars),
            "reviewed_bars must cover every performed bar exactly once, including silent bars")
    heads = {}
    for h in project["score_heads"]:
        require(isinstance(h["id"], str) and h["id"], "head IDs must be nonempty strings")
        require(h["id"] not in heads, f"duplicate head ID {h['id']}")
        require(str(h["bar"]) in bars, f"unknown bar for head {h['id']}")
        integer(h["pitch"], 0, 127, "head pitch")
        require(h["channel"] in channels, f"unknown head channel {h['id']}")
        heads[h["id"]] = h
    require(bool(heads), "No score heads; this workflow requires a reviewed score inventory")
    notes, coverage, deviations = {}, {}, []
    keys = collections.defaultdict(list)
    for n in project["notes"]:
        ident = n["id"]
        require(isinstance(ident, str) and ident and ident not in notes, "note IDs must be unique strings")
        integer(n["pitch"], 0, 127, "pitch")
        integer(n["velocity"], 1, 127, "velocity")
        require(n["channel"] in channels, f"unknown channel for {ident}")
        start, end = clock.tick(n["onset_seconds"]), clock.tick(n["key_release_seconds"])
        require(end > start, f"{ident}: key release must be after attack even after tick quantization")
        require(n.get("source") in ("score", "recording"), f"{ident}: source must be score or recording")
        sources = n["source_heads"]
        require(isinstance(sources, list), f"{ident}: source_heads must be a list")
        if n["source"] == "score":
            require(bool(sources), f"{ident}: score note requires source_heads")
        else:
            require(not sources and str(n.get("evidence", "")).strip(),
                    f"{ident}: recording supplement needs evidence and no score source_heads")
        if len(sources) > 1:
            require(n.get("source_relation") in ("tie", "shared", "tie_and_shared"),
                    f"{ident}: explain multiple source heads using source_relation")
        for sid in sources:
            require(sid in heads and sid not in coverage, f"{ident}: unknown or multiply mapped head {sid}")
            h = heads[sid]
            if (h["pitch"], h["channel"]) != (n["pitch"], n["channel"]):
                require(str(n.get("deviation_reason", "")).strip(),
                        f"{ident}: source pitch/channel changed without deviation_reason")
                deviations.append({"head": sid, "note": ident, "reason": n["deviation_reason"]})
            coverage[sid] = ident
        notes[ident] = n
        keys[n["pitch"]].append((start, end, ident))
    require(bool(notes), "notes is empty")
    for pitch, group in keys.items():
        group.sort()
        for prior, later in zip(group, group[1:]):
            require(prior[1] <= later[0],
                    f"physical key {pitch} overlaps across tracks: {prior[2]} / {later[2]}; review shared heads/restrike")
    omissions = {}
    for item in project.get("omitted_heads", []):
        sid = item["head_id"]
        require(sid in heads and sid not in coverage and sid not in omissions,
                f"invalid/duplicate omission {sid}")
        require(str(item.get("reason", "")).strip(), f"omitted head {sid} needs a reason")
        omissions[sid] = item["reason"]
    require(set(coverage) | set(omissions) == set(heads),
            f"Unaccounted score heads: {sorted(set(heads) - set(coverage) - set(omissions))[:25]}")
    require(type(project.get("pedal_expected")) is bool, "pedal_expected must be true or false")
    previous_tick, state = -1, False
    down_count = 0
    for event in project["pedal"]:
        tick = clock.tick(event["time_seconds"])
        integer(event["value"], 0, 127, "CC64 value")
        require(tick > previous_tick, "pedal events must have distinct increasing ticks; review zero-length changes")
        if event["value"] >= 64 and not state:
            down_count += 1
        state = event["value"] >= 64
        previous_tick = tick
    require(not state, "pedal curve ends pressed; add the observed/estimated release")
    if project["pedal_expected"]:
        require(down_count > 0, "pedal_expected but no pedal-down interval supplied")
    active = {n["channel"] for n in notes.values()}
    require(active == set(channels), "Declare only channels with physical note attacks")
    return clock, {"score_heads": len(heads), "mapped_heads": len(coverage),
                   "omitted_heads": omissions, "documented_deviations": deviations,
                   "attacks": len(notes), "bars_reviewed": len(reviewed),
                   "pedal_cycles_per_channel": down_count, "channels": sorted(channels)}


def export_project(project, target):
    clock, _ = validate_project(project)
    mid = mido.MidiFile(type=1, ticks_per_beat=clock.tpq, charset="utf8")
    last_second = max([n["key_release_seconds"] for n in project["notes"]]
                      + [p["time_seconds"] for p in project["pedal"]])
    end_second = project.get("end_audio_seconds", last_second + 2)
    require(end_second >= last_second, "end_audio_seconds precedes a key/pedal release")
    end_tick = clock.tick(end_second)

    def add_track(events):
        track = mido.MidiTrack()
        mid.tracks.append(track)
        previous = 0
        for tick, priority, msg in sorted(events, key=lambda e: (e[0], e[1])):
            require(0 <= tick <= end_tick, "event outside MIDI timeline")
            track.append(msg.copy(time=tick - previous))
            previous = tick
        track.append(mido.MetaMessage("end_of_track", time=end_tick - previous))

    meta = [(0, 0, mido.MetaMessage("track_name", name=project.get("title", "Reconstructed Piano")))]
    meta += [(round(b * clock.tpq), 1, mido.MetaMessage("set_tempo", tempo=t))
             for b, t in zip(clock.beats, clock.tempos)]
    for meter in project.get("meters", [{"beat": 0, "numerator": 4, "denominator": 4}]):
        meta.append((round(meter["beat"] * clock.tpq), 2,
                     mido.MetaMessage("time_signature", numerator=meter["numerator"], denominator=meter["denominator"])))
    for key in project.get("key_signatures", []):
        meta.append((round(key["beat"] * clock.tpq), 3, mido.MetaMessage("key_signature", key=key["key"])))
    for bar in project["bars"]:
        meta.append((round(bar["beat"] * clock.tpq), 4, mido.MetaMessage("marker", text="Bar " + str(bar["id"]))))
    add_track(meta)
    for info in project["channels"]:
        ch = info["channel"]
        events = [(0, 0, mido.MetaMessage("track_name", name=info.get("name", f"Piano {ch}"))),
                  (0, 0, mido.Message("program_change", channel=ch, program=info.get("program", 0)))]
        for control, value in ((121, 0), (7, 100), (11, 127), (10, 64), (64, 0)):
            events.append((0, 0, mido.Message("control_change", channel=ch, control=control, value=value)))
        for pedal in project["pedal"]:
            value = pedal["value"]
            events.append((clock.tick(pedal["time_seconds"]), 1 if value < 64 else 3,
                           mido.Message("control_change", channel=ch, control=64, value=value)))
        for note in project["notes"]:
            if note["channel"] != ch:
                continue
            events.append((clock.tick(note["onset_seconds"]), 4,
                           mido.Message("note_on", channel=ch, note=note["pitch"], velocity=note["velocity"])))
            events.append((clock.tick(note["key_release_seconds"]), 2,
                           mido.Message("note_off", channel=ch, note=note["pitch"], velocity=0)))
        events.append((end_tick, 5, mido.Message("control_change", channel=ch, control=64, value=0)))
        add_track(events)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    mid.save(target)


def inspect_midi(path):
    mid = mido.MidiFile(path, charset="utf8")
    require(mid.type in (0, 1), "Asynchronous type-2 MIDI is outside this piano workflow")
    held, deferred = {}, collections.defaultdict(set)
    pedal = collections.defaultdict(int)
    notes, cc64, tempos, meters, markers, programs, key_signatures = [], [], [], [], [], [], []
    down, up, captured = collections.Counter(), collections.Counter(), collections.Counter()
    time = 0.0
    for msg in mid:
        require(msg.time >= 0, "negative MIDI delta")
        time += msg.time
        if msg.type == "note_on" and msg.velocity:
            key = (msg.channel, msg.note)
            require(key not in held, f"overlapping same-channel note {key}")
            held[key] = (time, msg.velocity)
            deferred[msg.channel].discard(msg.note)
        elif msg.type == "note_off" or (msg.type == "note_on" and not msg.velocity):
            key = (msg.channel, msg.note)
            require(key in held, f"unpaired note-off {key}")
            onset, velocity = held.pop(key)
            require(time > onset, f"zero/negative duration {key}")
            notes.append((msg.channel, msg.note, onset, time, velocity))
            if pedal[msg.channel] >= 64:
                deferred[msg.channel].add(msg.note)
                captured[msg.channel] += 1
        elif msg.type == "control_change" and msg.control == 64:
            ch, value = msg.channel, msg.value
            cc64.append((ch, time, value))
            if value >= 64 and pedal[ch] < 64:
                down[ch] += 1
            if value < 64:
                if pedal[ch] >= 64:
                    up[ch] += 1
                deferred[ch].clear()
            pedal[ch] = value
        elif msg.type == "control_change" and msg.control == 121:
            pedal[msg.channel] = 0
            deferred[msg.channel].clear()
        elif msg.type == "set_tempo":
            tempos.append((time, msg.tempo))
        elif msg.type == "time_signature":
            meters.append((time, msg.numerator, msg.denominator))
        elif msg.type == "marker":
            markers.append((time, msg.text))
        elif msg.type == "program_change":
            programs.append((msg.channel, time, msg.program))
        elif msg.type == "key_signature":
            key_signatures.append((time, msg.key))
    require(not held, "stuck MIDI keys at end")
    require(not any(deferred.values()) and not any(v >= 64 for v in pedal.values()), "stuck MIDI pedal at end")
    require(down == up, "unbalanced pedal cycles")
    return {"notes": sorted(notes), "cc64": cc64, "tempos": tempos, "meters": meters,
            "markers": markers, "programs": programs, "key_signatures": key_signatures,
            "duration_seconds": time, "ticks_per_beat": mid.ticks_per_beat,
            "pedal_down": dict(down), "pedal_up": dict(up), "captured_key_releases": dict(captured)}


def verify_project(project, midi, baseline=None):
    clock, report = validate_project(project)
    saved = inspect_midi(midi)
    require(saved["ticks_per_beat"] == clock.tpq, "saved TPQ differs")
    expected = sorted((n["channel"], n["pitch"], n["onset_seconds"] - clock.origin,
                       n["key_release_seconds"] - clock.origin, n["velocity"]) for n in project["notes"])
    require(len(expected) == len(saved["notes"]), "saved note count differs")
    tolerance = max(clock.tempos) / 1e6 / clock.tpq * 0.51 + 1e-7
    errors = []
    for a, b in zip(expected, saved["notes"]):
        require(a[:2] == b[:2] and a[4] == b[4], "saved note pitch/channel/velocity differs")
        errors.extend((abs(a[2] - b[2]), abs(a[3] - b[3])))
    require(max(errors) <= tolerance, "saved note timing differs beyond tick rounding")
    require([t for _, t in saved["tempos"]] == clock.tempos, "saved tempo map differs")
    for (time, _), beat in zip(saved["tempos"], clock.beats):
        require(abs(time - (clock.audio_seconds(round(beat * clock.tpq)) - clock.origin)) < 1e-6,
                "saved tempo position differs")
    expected_programs = sorted((c["channel"], 0.0, c.get("program", 0)) for c in project["channels"])
    require(sorted(saved["programs"]) == expected_programs, "saved instrument program differs")
    def meta_expected(items, fields):
        return [(clock.audio_seconds(round(item["beat"] * clock.tpq)) - clock.origin,
                 *[item[field] for field in fields]) for item in sorted(items, key=lambda x: x["beat"])]
    expected_meters = meta_expected(project.get("meters", [{"beat": 0, "numerator": 4, "denominator": 4}]),
                                   ["numerator", "denominator"])
    expected_keys = meta_expected(project.get("key_signatures", []), ["key"])
    expected_markers = [(clock.audio_seconds(round(b["beat"] * clock.tpq)) - clock.origin, "Bar " + str(b["id"]))
                        for b in sorted(project["bars"], key=lambda b: b["beat"])]
    for label, wanted in (("meters", expected_meters), ("key_signatures", expected_keys), ("markers", expected_markers)):
        require(len(saved[label]) == len(wanted), f"saved {label} count differs")
        for actual, expect in zip(saved[label], wanted):
            require(actual[1:] == expect[1:] and abs(actual[0] - expect[0]) <= tolerance, f"saved {label} differs")
    for ch in report["channels"]:
        found = [(time, value) for c, time, value in saved["cc64"] if c == ch]
        wanted = [(0.0, 0)] + [(p["time_seconds"] - clock.origin, p["value"]) for p in project["pedal"]]
        wanted.append((saved["duration_seconds"], 0))
        require(len(found) == len(wanted), f"CC64 count differs on channel {ch}")
        for (a, av), (b, bv) in zip(found, wanted):
            require(av == bv and abs(a - b) <= tolerance, f"CC64 curve differs on channel {ch}")
    if baseline is not None:
        previous = {n["id"]: n for n in baseline["notes"]}
        common = [n for n in project["notes"] if n["id"] in previous]
        require(project["tempo_map"] == baseline["tempo_map"] and clock.origin == baseline.get("origin_seconds", 0),
                "accepted tempo/origin changed")
        require(all(abs(n["onset_seconds"] - previous[n["id"]]["onset_seconds"]) < 1e-9 for n in common),
                "accepted note onset changed")
        report["baseline_onsets_preserved"] = len(common)
    warnings = []
    if project["pedal_expected"] and not sum(saved["captured_key_releases"].values()):
        warnings.append("CC64 exists but no key releases occur while held; review key lengths/pedal timing")
    report.update(midi=str(Path(midi).resolve()), midi_sha256=sha256(midi),
                  max_time_rounding_error_ms=max(errors) * 1000, no_stuck_keys_or_pedal=True,
                  pedal_down_per_channel=saved["pedal_down"], pedal_up_per_channel=saved["pedal_up"],
                  key_releases_under_pedal=saved["captured_key_releases"], warnings=warnings,
                  duration_seconds=saved["duration_seconds"])
    return report


def write_coverage(project, path):
    heads = {h["id"]: h for h in project["score_heads"]}
    mapping = {sid: n for n in project["notes"] for sid in n["source_heads"]}
    omitted = {o["head_id"]: o["reason"] for o in project.get("omitted_heads", [])}
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(["head_id", "printed_id", "bar", "score_pitch", "note_id", "midi_pitch", "channel",
                         "onset_seconds", "key_release_seconds", "relation_or_omission"])
        for sid, h in heads.items():
            n = mapping.get(sid)
            writer.writerow([sid, h.get("printed_id", sid), h["bar"], h["pitch"],
                             n["id"] if n else "", n["pitch"] if n else "", n["channel"] if n else "",
                             n["onset_seconds"] if n else "", n["key_release_seconds"] if n else "",
                             n.get("source_relation", "single") if n else omitted[sid]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["export", "verify"])
    parser.add_argument("project", type=Path)
    parser.add_argument("midi", type=Path)
    parser.add_argument("--baseline", type=Path, help="Require common note IDs, tempo map and origin to keep accepted timing")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing the specified output MIDI")
    args = parser.parse_args()
    project = read_json(args.project)
    if args.command == "export":
        require(args.overwrite or not args.midi.exists(), "Output exists; choose a new revision name or use --overwrite")
        require(args.midi.resolve() != args.project.resolve(), "MIDI output cannot replace project JSON")
        export_project(project, args.midi)
    report = verify_project(project, args.midi, read_json(args.baseline) if args.baseline else None)
    write_json(args.midi.with_suffix(".validation.json"), report)
    write_coverage(project, args.midi.with_suffix(".coverage.tsv"))
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
