# Third-party notices

The MIT license at the repository root covers PC Remote's original code.
It does not relicense third-party libraries, applications, or model weights.

## Bundled browser desktop client

`web/vendor/novnc` contains noVNC 1.4.0, pinned to upstream commit
`90455eef0692d2e35276fd31286114d0955016b0`, including its source, authors,
licenses, and bundled dependencies. noVNC is primarily licensed under MPL 2.0;
some files have different licenses. See its [license file](web/vendor/novnc/LICENSE.txt)
and individual source headers. The generated `rfb.bundle.js` is built from those
retained sources; `tools/Build-Desktop-Viewer.ps1` reproduces the bundle.

Upstream: https://github.com/novnc/noVNC/tree/v1.4.0

## Optional desktop server

TightVNC 2.8.88 is downloaded separately from the official publisher by
`Setup-Desktop-Viewer.ps1`. Its installer and extracted executable are not
included in this repository. TightVNC's GPL/commercial licensing and source
availability remain governed by its publisher:

- https://www.tightvnc.com/licensing.php
- https://www.tightvnc.com/download.php

The setup verifies the expected archive SHA-256 and Authenticode publisher,
extracts the files, and runs the server in the signed-in Windows user session.

## Optional speech

Kokoro-82M model weights are licensed under Apache 2.0. The kokoro-onnx runtime
is MIT licensed. Optional setup downloads checksum-verified model and voice
assets separately; they are not included in this repository. Runtime package
dependencies retain their respective licenses.

- https://huggingface.co/hexgrad/Kokoro-82M
- https://github.com/thewh1teagle/kokoro-onnx

The optional pronunciation stack includes GPL-3.0 licensed eSpeak NG and
phonemizer components. Installing the optional runtime does not change their
licenses. Their source and notices are available upstream; anyone redistributing
that runtime must comply with the applicable dependency licenses:

- https://github.com/espeak-ng/espeak-ng
- https://github.com/bootphon/phonemizer
- https://github.com/thewh1teagle/espeakng-loader

Whisper/faster-whisper transcription dependencies and model files are optional
and separately installed. The dictation setup pins Systran's faster-whisper-small
revision `536b0662742c02347bc0e980a01041f333bce120` and verifies the downloaded
files. See https://github.com/SYSTRAN/faster-whisper and
https://huggingface.co/Systran/faster-whisper-small for source and model notices.

## Other dependencies and applications

Python dependencies listed in `requirements.txt` and optional setup requirements
retain their upstream licenses. esbuild is MIT licensed and downloaded only
when rebuilding the browser bundle. 7-Zip, LM Studio, ComfyUI, Tailscale, and
the ChatGPT/Codex desktop application are separate products and are not
redistributed as part of PC Remote. Consult each product's terms before use
or redistribution. PC Remote is an independent project and is not affiliated
with those products or with MLS Innovation.
