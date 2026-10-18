"""Prepare local score/audio evidence without interpreting notation or transcribing.

Originals are read-only. PDF render/objects and decoded PCM go in a fresh folder.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import shutil
import subprocess
import wave
from pathlib import Path


def fingerprint(path):
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": digest}


def ffmpeg_exe(explicit=None):
    if explicit:
        return str(explicit)
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as error:
        raise RuntimeError("Use --ffmpeg PATH or install imageio-ffmpeg") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score", type=Path, required=True, help="PDF, image, MusicXML or MXL")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="A new work directory")
    parser.add_argument("--sample-rate", type=int, default=22050)
    parser.add_argument("--dpi", type=int, default=180)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--skip-pdf-objects", action="store_true")
    args = parser.parse_args()
    if not args.score.is_file() or not args.audio.is_file():
        parser.error("Both original inputs must be existing files")
    if args.out.exists():
        parser.error("Output directory exists; use a fresh work directory to preserve earlier results")
    if not 8000 <= args.sample_rate <= 192000 or not 72 <= args.dpi <= 600:
        parser.error("Choose sample rate 8000..192000 and DPI 72..600")
    ffmpeg = ffmpeg_exe(args.ffmpeg)
    args.out.mkdir(parents=True)
    manifest = {"score": fingerprint(args.score), "audio": fingerprint(args.audio), "warnings": []}
    if args.score.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(str(args.score))
        pages = []
        for index in range(len(document)):
            page = document[index]
            target = args.out / f"score-page-{index + 1:03}.png"
            bitmap = page.render(scale=args.dpi / 72)
            bitmap.to_pil().save(target)
            pages.append({"page": index + 1, "size_points": list(page.get_size()), "image": target.name})
            bitmap.close()
            page.close()
        document.close()
        manifest["pages"] = pages
        if not args.skip_pdf_objects:
            import pdfplumber
            objects = []
            with pdfplumber.open(args.score) as pdf:
                for index, page in enumerate(pdf.pages):
                    objects.append({"page": index + 1, "width": page.width, "height": page.height,
                                    "chars": page.chars, "lines": page.lines,
                                    "curves": page.curves, "rects": page.rects})
                    pages[index]["fonts"] = dict(collections.Counter(c["fontname"] for c in page.chars))
                    pages[index]["char_count"] = len(page.chars)
            (args.out / "pdf_objects.json").write_text(
                json.dumps(objects, ensure_ascii=False, default=str), encoding="utf-8")
            if not any(p["char_count"] for p in pages):
                manifest["warnings"].append("No PDF characters: use image/OMR review, not font parsing")
    else:
        manifest["score_route"] = "Inspect image directly, or parse MusicXML with visual cross-check"
    decoded = args.out / "analysis.wav"
    subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-n", "-i", str(args.audio),
                    "-vn", "-ac", "1", "-ar", str(args.sample_rate), "-c:a", "pcm_s16le", str(decoded)], check=True)
    with wave.open(str(decoded), "rb") as pcm:
        manifest["analysis_audio"] = {"path": decoded.name, "sample_rate": pcm.getframerate(),
                                      "channels": pcm.getnchannels(), "duration_seconds": pcm.getnframes() / pcm.getframerate()}
    manifest["ffmpeg_version"] = subprocess.check_output(
        [ffmpeg, "-version"], text=True, encoding="utf-8", errors="replace").splitlines()[0]
    (args.out / "input_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
