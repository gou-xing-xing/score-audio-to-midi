"""Suggest same-pitch score/audio matches, allowing missed and extra attacks.

Never edits the project. Unmatched notes are review candidates, not deletions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment


def match(project, audio, window=0.25):
    if not np.isfinite(window) or window <= 0:
        raise ValueError("Matching window must be positive")
    notes = project["notes"]
    events = audio["notes"] if isinstance(audio, dict) else audio
    candidates = []
    for index, item in enumerate(events):
        pitch = item.get("pitch", item.get("midi_note"))
        onset = item.get("onset_seconds", item.get("onset_time"))
        if type(pitch) is not int or not 0 <= pitch <= 127 or onset is None or not np.isfinite(onset):
            raise ValueError(f"Invalid audio note {index}: require pitch and finite onset seconds")
        candidates.append({"index": index, "pitch": pitch, "onset_seconds": float(onset)})
    result, used_score, used_audio = [], set(), set()
    for pitch in sorted({n["pitch"] for n in notes}):
        si = [i for i, n in enumerate(notes) if n["pitch"] == pitch]
        ai = [i for i, n in enumerate(candidates) if n["pitch"] == pitch]
        if not ai:
            continue
        distance = np.abs(np.array([notes[i]["onset_seconds"] for i in si])[:, None]
                          - np.array([candidates[i]["onset_seconds"] for i in ai])[None, :])
        # Two unmatched items cost the full window; out-of-window edges cannot
        # steal a match. Dummy nodes prevent missing reattacks shifting a run.
        costs = np.full((len(si) + len(ai), len(si) + len(ai)), window * 1000)
        costs[:len(si), :len(ai)] = np.where(distance <= window, distance, window * 1000)
        costs[:len(si), len(ai):] = window * 0.500001
        costs[len(si):, :len(ai)] = window * 0.500001
        costs[len(si):, len(ai):] = 0
        rows, columns = linear_sum_assignment(costs)
        for row, col in zip(rows, columns):
            if row >= len(si) or col >= len(ai) or distance[row, col] > window:
                continue
            s, a = si[row], ai[col]
            used_score.add(s)
            used_audio.add(a)
            result.append({"note_id": notes[s]["id"], "audio_index": a, "pitch": pitch,
                           "score_onset_seconds": notes[s]["onset_seconds"],
                           "audio_onset_seconds": candidates[a]["onset_seconds"],
                           "difference_ms": (candidates[a]["onset_seconds"] - notes[s]["onset_seconds"]) * 1000})
    return {"window_seconds": window, "matches": result,
            "score_unmatched": [n for i, n in enumerate(notes) if i not in used_score],
            "audio_unmatched": [n for i, n in enumerate(candidates) if i not in used_audio],
            "meaning": "Candidates for review only. Low confidence or missing audio detection does not justify score deletion."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("audio_notes", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--window", type=float, default=0.25)
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve() in (args.project.resolve(), args.audio_notes.resolve()):
        parser.error("Choose a new output file")
    project = json.loads(args.project.read_text(encoding="utf-8-sig"))
    audio = json.loads(args.audio_notes.read_text(encoding="utf-8-sig"))
    result = match(project, audio, args.window)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: len(result[k]) for k in ("matches", "score_unmatched", "audio_unmatched")}))


if __name__ == "__main__":
    main()
