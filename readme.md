# StudioScreenRecorder

StudioScreenRecorder is a Windows desktop screen recorder built with Python,
`customtkinter`, FFmpeg, SoundCard, NumPy, and SciPy. It combines smooth desktop
capture with synchronized system loopback audio, microphone recording, live
audio cleanup, and recoverable MP4 output in one easy-to-use application.

## Features

- Records the Windows desktop at a target rate of 60 FPS using FFmpeg `gdigrab`
- Captures system audio through the active speaker's loopback device
- Captures and mixes microphone audio alongside system sound
- Applies low-frequency rumble removal and smooth audio block transitions
- Detects default audio-device changes and reconnects during recording
- Writes fragmented MP4 data continuously for improved crash resilience
- Finalizes completed recordings into standard, player-friendly MP4 files
- Provides a responsive desktop interface with recording and folder controls
- Includes direct links to official downloads, source code, and author profiles

## How It Works

The recording engine launches FFmpeg as a background process for desktop video
capture and encoding. Separate audio workers collect system and microphone
samples, normalize their channel layout, smooth discontinuities, and feed a
synchronized mixed stream into FFmpeg. Queue limits and missing-block
concealment help the application remain responsive when Windows audio devices
briefly pause or change.

During capture, output is written as a fragmented MP4 so useful recording data
is preserved progressively. When recording stops normally, StudioScreenRecorder
remuxes that file into a standard MP4 with fast-start metadata for broad media
player compatibility.

## Installation

Install from PyPI:

```bash
pip install studioscreenrecorder
```

## Run

After installation:

```bash
studioscreenrecorder
```

Choose a destination folder, enable the capture sources you need, and select
**START**. Select **STOP** to finalize and save the recording.

## Official Links

- [StudioScreenRecorder on PyPI](https://pypi.org/project/studioscreenrecorder/)
- [PyPI publisher profile](https://pypi.org/user/brightlyyy/)
- [Source code repository](https://github.com/madhavabrightly/screenrecorder-fullpython)
- [Madhava Brightly on GitHub](https://github.com/madhavabrightly)
- [Madhava Brightly on Gravatar](https://gravatar.com/madhavabrightly)

## Author

StudioScreenRecorder is created and maintained by Madhava Brightly. Bug reports,
feature discussions, and contributions can be submitted through the public
GitHub repository.
