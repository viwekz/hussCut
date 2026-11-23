"""Turn NVIDIA's Arabic speech model into the files PeakCut's speech library reads, and try it on real videos.

This is run once by whoever publishes the Arabic model, not by people using PeakCut. PeakCut itself only downloads
the finished archive this makes.

  1. Make the model files (needs a separate Python with NVIDIA's NeMo toolkit; Linux or Google Colab is easiest):
       pip install "nemo_toolkit[asr]" onnx onnxruntime
       python tools/export_arabic_model.py export
     This writes sherpa-onnx-nemo-fastconformer-ar-int8/ and sherpa-onnx-nemo-fastconformer-ar-int8.tar.bz2.

  2. Try it on a few Arabic videos or audio files (in PeakCut's own Python, with ffmpeg):
       python tools/export_arabic_model.py try sherpa-onnx-nemo-fastconformer-ar-int8 news.mp4 vlog.mp4 ...
     It prints what it heard, with the time of each word, so you can judge it by eye.

The way the model is exported follows the sherpa-onnx project's own recipe for this family of NVIDIA models
(scripts/nemo/fast-conformer-hybrid-transducer-ctc in github.com/k2-fsa/sherpa-onnx, Apache 2.0). Only the
"transducer" half of the model is kept: it is the same kind PeakCut already uses for English.

The model is NVIDIA's stt_ar_fastconformer_hybrid_large_pc_v1.0 (Arabic with punctuation, no vowel marks),
licensed CC-BY-4.0: it may be shared and used commercially as long as NVIDIA is credited, which the README inside
the archive does."""
import argparse
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

SOURCE = "nvidia/stt_ar_fastconformer_hybrid_large_pc_v1.0"
NAME = "sherpa-onnx-nemo-fastconformer-ar-int8"

CREDIT = f"""---
license: cc-by-4.0
language:
- ar
base_model: {SOURCE}
pipeline_tag: automatic-speech-recognition
tags:
- sherpa-onnx
- nemo
---

# Arabic speech model for PeakCut

Converted for sherpa-onnx from NVIDIA's **{SOURCE}**
(https://huggingface.co/{SOURCE}), released by NVIDIA under the
Creative Commons Attribution 4.0 licence (CC-BY-4.0, https://creativecommons.org/licenses/by/4.0/).

Changes: only the transducer branch was exported to ONNX and its weights were quantized to 8 bits,
following the sherpa-onnx recipe for NeMo FastConformer hybrid models. Nothing was retrained.
"""


def _add_meta(path: Path, meta: dict):
    """Write the notes the speech library reads (vocabulary size, how the sound is normalised...) into a model file."""
    import onnx
    m = onnx.load(str(path))
    while len(m.metadata_props):
        m.metadata_props.pop()
    for k, v in meta.items():
        p = m.metadata_props.add()
        p.key, p.value = k, str(v)
    onnx.save(m, str(path))


def export(out: Path):
    import nemo.collections.asr as nemo_asr
    import torch
    from onnxruntime.quantization import QuantType, quantize_dynamic

    work = out / "full"
    shutil.rmtree(out, ignore_errors=True)
    work.mkdir(parents=True)
    model = nemo_asr.models.ASRModel.from_pretrained(model_name=SOURCE)

    vocab = list(model.joint.vocabulary)
    with open(out / "tokens.txt", "w", encoding="utf-8") as f:
        for i, s in enumerate(vocab):
            f.write(f"{s} {i}\n")
        f.write(f"<blk> {len(vocab)}\n")
    starts = sum(1 for s in vocab if s.startswith("▁"))
    print(f"Word list: {len(vocab)} pieces, {starts} of them start a word with '▁'.")
    if not starts:
        print("WARNING: no piece starts with '▁'. PeakCut finds word boundaries that way, so check before publishing.")

    model.change_decoding_strategy(decoder_type="rnnt")
    model.eval()
    model.set_export_config({"decoder_type": "rnnt"})
    with torch.no_grad():
        model.encoder.export(str(work / "encoder.onnx"))
        model.decoder.export(str(work / "decoder.onnx"))
        model.joint.export(str(work / "joiner.onnx"))

    normalize = model.cfg.preprocessor.normalize
    _add_meta(work / "encoder.onnx", {
        "vocab_size": model.decoder.vocab_size,
        "normalize_type": "" if normalize == "NA" else normalize,
        "pred_rnn_layers": model.decoder.pred_rnn_layers,
        "pred_hidden": model.decoder.pred_hidden,
        "subsampling_factor": 8,
        "model_type": "EncDecHybridRNNTCTCBPEModel",
        "version": "1",
        "model_author": "NeMo",
        "url": f"https://huggingface.co/{SOURCE}",
        "comment": "Only the transducer branch is exported",
    })
    for part in ("encoder", "decoder", "joiner"):
        quantize_dynamic(model_input=str(work / f"{part}.onnx"), model_output=str(out / f"{part}.int8.onnx"),
                         weight_type=QuantType.QUInt8)
    shutil.rmtree(work)
    (out / "README.md").write_text(CREDIT, encoding="utf-8")

    archive = out.parent / f"{NAME}.tar.bz2"
    with tarfile.open(archive, "w:bz2") as t:
        t.add(out, arcname=NAME)       # PeakCut expects one folder named after the model inside the archive
    size = sum(p.stat().st_size for p in out.iterdir()) / 1e6
    print(f"Done: {out} ({size:.0f} MB), packed as {archive} ({archive.stat().st_size / 1e6:.0f} MB).")


def try_files(folder: Path, files: list):
    """Transcribe each file with the exported model and print the words with their times."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from studio import media, transcribe
    sherpa = transcribe._sherpa()
    rec = sherpa.OfflineRecognizer.from_transducer(
        **transcribe._model_files(folder), tokens=str(folder / "tokens.txt"), num_threads=transcribe.THREADS,
        sample_rate=16000, feature_dim=80, decoding_method="greedy_search", model_type="nemo_transducer")
    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            _try_one(rec, transcribe, media, Path(f), Path(tmp) / "audio.wav")


def _try_one(rec, transcribe, media, f: Path, wav: Path):
    media.extract_audio(f, wav)            # any video or audio file -> 16 kHz mono, as PeakCut does
    audio = media.load_audio(wav)
    took = time.time()
    words = []
    for a, b in transcribe._chunks(audio):
        seg = audio[a:b].astype("float32") / 32768.0
        if len(seg) < 4800:
            continue
        st = rec.create_stream()           # decoded here directly, so PeakCut's own English model is never touched
        st.accept_waveform(16000, seg)
        rec.decode_stream(st)
        words += transcribe._words(list(st.result.tokens), [float(t) for t in st.result.timestamps], a, b)
    took = time.time() - took
    length = len(audio) / 16000
    print(f"\n=== {f}: {length:.0f} s of audio, transcribed in {took:.0f} s ({length / max(took, 0.1):.0f}x real time)")
    line, start = [], None
    for w in words:
        start = w["s"] if start is None else start
        line.append(w["w"])
        if len(line) == 12:
            print(f"[{start:7.2f}] {' '.join(line)}")
            line, start = [], None
    if line:
        print(f"[{start:7.2f}] {' '.join(line)}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="make the model files and the archive")
    e.add_argument("--out", default=NAME)
    t = sub.add_parser("try", help="transcribe some files with the exported model")
    t.add_argument("folder")
    t.add_argument("files", nargs="+")
    a = p.parse_args()
    if a.cmd == "export":
        export(Path(a.out).resolve())
    else:
        try_files(Path(a.folder).resolve(), a.files)


if __name__ == "__main__":
    main()
