---
title: 'StudioScreenRecorder: Recoverable Windows desktop recording with loopback and microphone audio'
tags:
  - Python
  - screen recording
  - FFmpeg
  - loopback audio
  - research software
authors:
  - name: Madhava Brightly
    affiliation: "1"
    email: madhavabrightly@gmail.com
affiliations:
  - name: Independent researcher, India
    index: 1
date: 17 June 2026
bibliography: paper.bib
---

# Summary

StudioScreenRecorder is an open-source Windows desktop recorder implemented in
Python. It records the desktop at a target rate of 60 frames per second,
captures system sound from the active speaker loopback device, optionally
captures microphone input, and writes the result to MP4. The software combines a
small CustomTkinter graphical interface with FFmpeg desktop capture, SoundCard
audio acquisition, and NumPy/SciPy-based audio block handling
[@ffmpeg_devices; @soundcard; @customtkinter; @harris2020array; @virtanen2020scipy].

The program is designed for users who need a focused recorder rather than a
full broadcasting studio. Typical use cases include recording software
experiments, debugging sessions, online lectures, tutorials, usability tests,
and demonstrations where desktop activity, application sound, and spoken
commentary must be captured together. StudioScreenRecorder is distributed on
PyPI as `studioscreenrecorder` and the public source repository is hosted on
GitHub [@studioscreenrecorder_pypi; @studioscreenrecorder_repo].

# Statement of need

Desktop recordings are often used as research and engineering evidence. They
can document user-study sessions, software demonstrations, machine-learning
training workflows, bug reproduction, classroom instruction, and evaluation
procedures. In these settings, a failed or incomplete recording can be costly:
the original interaction may not be repeatable, and missing audio or an
unplayable MP4 can make the evidence unusable.

Existing screen-recording tools are powerful, but they do not always match the
needs of a lightweight Python-centered workflow. Researchers and students may
want a simple installable application that exposes the essential recording
controls, uses open multimedia components, and has an internal architecture that
can be inspected, modified, and benchmarked. StudioScreenRecorder addresses this
need by providing a compact recorder whose implementation is understandable to
Python users while still relying on established upstream tools for media capture
and encoding.

The target audience is researchers, research software engineers, educators, and
students who need reproducible desktop recordings on Windows, especially when
system audio and microphone audio must be captured at the same time. The
software is not intended to replace professional scene-composition or streaming
systems. Its purpose is narrower: reliable local recording with clear source
code, simple packaging, and recoverability-oriented output handling.

# State of the field

OBS Studio is the most capable related open-source system. It supports scenes,
streaming, advanced audio routing, multi-track output, and recoverable
recording formats [@obs_quickstart; @obs_hybrid; @obs_audio]. ShareX provides a
Windows productivity environment for screenshots, screen recording, upload
workflows, OCR, and automation [@sharex_site; @sharex_repo]. ScreenToGif offers
lightweight selected-area recording with editing and export options
[@screentogif_repo].

StudioScreenRecorder does not compete with those tools on breadth. The
"build vs. contribute" justification is that the project explores a different
software shape: a compact Python package and GUI focused on FFmpeg `gdigrab`,
SoundCard loopback capture, bounded audio queues, default-device reconnection,
and recoverable MP4 handling. This makes the code useful as a research-software
artifact for studying desktop-capture reliability in a Python application, and
as a modifiable recorder for projects that prefer a small Python codebase over
large existing applications.

# Software design

The design separates responsibilities between Python and FFmpeg. FFmpeg handles
desktop capture and H.264/AAC encoding using the Windows `gdigrab` input device
and MP4 muxing [@ffmpeg_devices; @ffmpeg_formats]. Python manages the user
interface, output paths, device discovery, audio workers, queueing, and
recording lifecycle.

Each enabled audio source runs in its own worker thread. System audio is
captured from the active speaker's loopback endpoint, while microphone audio is
captured from the active default microphone through SoundCard [@soundcard].
Blocks are normalized to a common 48 kHz stereo format, placed into bounded
queues, mixed by a feeder thread, and written to FFmpeg through standard input.
The bounded queues are a deliberate trade-off: under stress, the program favors
recent audio and bounded latency rather than accumulating an ever-growing
backlog.

The software also watches for default audio-device changes. If the active
speaker or microphone changes during recording, the corresponding worker
reopens the new device instead of keeping a stale handle. This matters for
common Windows workflows where headphones, USB microphones, Bluetooth devices,
or speaker outputs are connected and disconnected during a session.

For video, StudioScreenRecorder uses constant-frame-rate output and conservative
encoding settings so that the generated MP4 is broadly playable. The output
strategy emphasizes survivability: recording data is written progressively and
normal stop behavior finalizes the file into a standard player-friendly MP4.
This design keeps the code simple while reducing the chance that a long
recording becomes useless because finalization fails.

# Research impact statement

The current public release is `studioscreenrecorder` version 1.0.2 on PyPI,
released on 13 June 2026, under an MIT license [@studioscreenrecorder_pypi].
The GitHub repository contains the source code, license, package metadata, PyPI
publishing workflow, and paper draft [@studioscreenrecorder_repo]. Initial
validation on a 1920 by 1080 Windows desktop showed successful video-only and
system-audio recordings with FFmpeg-reported 60.0 FPS output. These early tests
support the feasibility of the architecture, but broader external adoption and
long-duration benchmark results are still limited.

The near-term research significance is in the reproducible architecture and
evaluation path. StudioScreenRecorder can be used to record experiments and can
itself be benchmarked for frame rate, audio continuity, device-switch recovery,
finalization latency, and output integrity. Future repository work should add
automated tests, published benchmark logs, and reproducible example recordings
so that reviewers and users can objectively compare its reliability against
larger recorders.

# AI usage disclosure

Generative AI tools, including OpenAI ChatGPT/Codex, were used during parts of
software development and manuscript preparation. Assistance included code review,
refactoring suggestions, packaging guidance, and editing this JOSS paper into the
required structure and length. The author reviewed and modified the resulting
code and text, made the final design decisions, and remains responsible for the
correctness, scope, and claims of the submission.

# Acknowledgements

StudioScreenRecorder builds on FFmpeg, SoundCard, NumPy, SciPy, imageio-ffmpeg,
CustomTkinter, PyPI, and GitHub. No direct financial support was received for
the development of this software.

# References
