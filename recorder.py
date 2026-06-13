import ctypes
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import warnings
from pathlib import Path
from typing import Any, Optional
from collections import deque


FOLDER_PICKER_ARG = "--studio-capture-folder-picker"
POWERSHELL_FOLDER_PICKER = r"""
& {
    param([string]$initialDirectory, [string]$resultFile)
    $ErrorActionPreference = 'Stop'
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.Application]::EnableVisualStyles()
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    try {
        $dialog.Description = 'Select the folder where recordings will be saved'
        $dialog.SelectedPath = $initialDirectory
        $dialog.ShowNewFolderButton = $true
        if ($null -ne $dialog.PSObject.Properties['AutoUpgradeEnabled']) {
            $dialog.AutoUpgradeEnabled = $true
        }
        if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            $utf8 = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText($resultFile, $dialog.SelectedPath, $utf8)
        }
    }
    finally {
        $dialog.Dispose()
    }
}
"""


def run_folder_picker_helper() -> int:
    """Run the potentially slow Windows folder dialog outside the main UI process."""
    import tkinter as tk
    from tkinter import filedialog

    if len(sys.argv) < 4:
        return 2

    root = tk.Tk()
    root.withdraw()
    try:
        try:
            root.attributes("-topmost", True)
            root.lift()
            root.focus_force()
            root.update()
        except Exception:
            pass
        folder = filedialog.askdirectory(
            title="Select the folder where recordings will be saved",
            initialdir=sys.argv[2],
            mustexist=True,
        )
        if folder:
            Path(sys.argv[3]).write_text(folder, encoding="utf-8")
        return 0
    except Exception:
        log_exception("Folder picker helper failed")
        return 1
    finally:
        root.destroy()


def folder_picker_command(initial_directory: Path, result_file: Path) -> list[str]:
    """Build a helper command that keeps folder selection out of the main UI process."""
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    command = [str(pythonw if pythonw.exists() else executable)]
    if not getattr(sys, "frozen", False):
        command.append(str(Path(__file__).resolve()))
    command.extend([FOLDER_PICKER_ARG, str(initial_directory), str(result_file)])
    return command


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == FOLDER_PICKER_ARG:
    raise SystemExit(run_folder_picker_helper())


import customtkinter as ctk
import imageio_ffmpeg
import numpy as np
import soundcard as sc
from tkinter import filedialog, messagebox
from scipy import signal
from scipy.fft import rfft, irfft


# Windows COM imports for native file dialog
if platform.system() == "Windows":
    try:
        from ctypes import wintypes
        
        # COM constants
        CLSCTX_INPROC_SERVER = 0x1
        FOS_PICKFOLDERS = 0x00000020
        FOS_FORCEFILESYSTEM = 0x00000040
        FOS_NOVALIDATE = 0x00000100
        FOS_NOTESTFILECREATE = 0x00010000
        FOS_DONTADDTORECENT = 0x02000000
        
        # IFileDialog interface GUIDs
        CLSID_FileOpenDialog = ctypes.GUID(0xDC1C5A9C, 0xE88A, 0x4DDE, (0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x20, 0xAE, 0xF7))
        IID_IFileOpenDialog = ctypes.GUID(0xD57C7288, 0xD4AD, 0x4768, (0xBE, 0x02, 0x9D, 0x96, 0x95, 0x32, 0xD9, 0x60))
        IID_IShellItem = ctypes.GUID(0x43826D1E, 0xE718, 0x42EE, (0xBC, 0x55, 0xA1, 0xE2, 0x61, 0xC3, 0x7B, 0xFE))
        IID_IFileDialogCustomize = ctypes.GUID(0xE6FDD21A, 0x163F, 0x4975, (0x9C, 0x8C, 0xA6, 0x9F, 0x1B, 0xA3, 0x70, 0x34))
        
        WIN_FILE_DIALOG_AVAILABLE = True
    except Exception:
        WIN_FILE_DIALOG_AVAILABLE = False
else:
    WIN_FILE_DIALOG_AVAILABLE = False


ctk.set_appearance_mode("dark")

APP_NAME = "StudioCapturePro"
APP_DIR = Path(os.getenv("LOCALAPPDATA", Path.home())) / APP_NAME
SETTINGS_FILE = APP_DIR / "settings.json"
LOG_FILE = APP_DIR / "studio_capture.log"
MIN_VALID_FILE_SIZE = 16 * 1024
MP4_FRAGMENT_DURATION_US = 1_000_000
SHORTEST_BUFFER_DURATION_SECONDS = 0.5
AUDIO_CAPTURE_BUFFER_MULTIPLIER = 8
AUDIO_DISCONTINUITY_LOG_INTERVAL = 10.0
AUDIO_BOUNDARY_FADE_SECONDS = 0.003
AUDIO_MISSING_FADE_SECONDS = 0.008


def ensure_app_directory() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def write_log(message: str) -> None:
    try:
        ensure_app_directory()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def log_exception(context: str) -> None:
    write_log(f"{context}\n{traceback.format_exc()}")


def load_settings() -> dict[str, Any]:
    defaults = {
        "save_directory": str(Path.home() / "Videos"),
        "capture_mouse": True,
        "capture_system_audio": True,
        "capture_microphone": True,
    }
    try:
        if SETTINGS_FILE.exists():
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            defaults.update({key: saved[key] for key in defaults if key in saved})
    except Exception:
        log_exception("Unable to load settings")
    return defaults


def save_settings(settings: dict[str, Any]) -> None:
    try:
        ensure_app_directory()
        temporary = SETTINGS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        os.replace(temporary, SETTINGS_FILE)
    except Exception:
        log_exception("Unable to save settings")


def directory_is_writable(directory: Path) -> tuple[bool, str]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".studio_capture_write_test_{os.getpid()}.tmp"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def unique_recording_path(directory: Path) -> Path:
    milliseconds = int((time.time() % 1) * 1000)
    stem = f"StudioRec_{time.strftime('%Y%m%d_%H%M%S')}_{milliseconds:03d}"
    candidate = directory / f"{stem}.mp4"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}.mp4"
        counter += 1
    return candidate


def resolve_ffmpeg() -> str:
    candidates: list[Path] = []

    try:
        candidates.append(Path(imageio_ffmpeg.get_ffmpeg_exe()))
    except Exception:
        log_exception("imageio-ffmpeg could not resolve FFmpeg")

    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                executable_dir / "ffmpeg.exe",
                executable_dir / "bin" / "ffmpeg.exe",
            ]
        )
        bundle_dir = getattr(sys, "_MEIPASS", None)
        if bundle_dir:
            candidates.extend(
                [
                    Path(bundle_dir) / "ffmpeg.exe",
                    Path(bundle_dir) / "imageio_ffmpeg" / "binaries" / "ffmpeg.exe",
                ]
            )

    path_ffmpeg = shutil.which("ffmpeg")
    if path_ffmpeg:
        candidates.append(Path(path_ffmpeg))

    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except Exception:
            continue
    raise FileNotFoundError("FFmpeg was not found. Include imageio-ffmpeg when building the EXE.")


class WindowsGlassEngine:
    @staticmethod
    def apply_acrylic_theme(window: ctk.CTk) -> None:
        if platform.system() != "Windows":
            return
        try:
            window.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
            backdrop = ctypes.c_int(3)
            dark_mode = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop)
            )
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(dark_mode), ctypes.sizeof(dark_mode)
            )
        except Exception:
            log_exception("Unable to apply Windows acrylic theme")


class FluidGlassButton(ctk.CTkCanvas):
    def __init__(
        self,
        parent: Any,
        text: str,
        command: Any,
        base_color: str = "#00bcd4",
        hover_color: str = "#00e5ff",
        width: int = 130,
        height: int = 36,
    ):
        super().__init__(
            parent, width=width, height=height, bg="#14161d", highlightthickness=0
        )
        self.command = command
        self.base_color = base_color
        self.hover_color = hover_color
        self.w, self.h = width, height
        self.text_str = text
        self.is_disabled = False
        self.current_scale, self.target_scale = 1.0, 1.0
        self.velocity, self.stiffness, self.damping = 0.0, 0.24, 0.56
        self._anim_id: Optional[str] = None
        self._render_element()
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", lambda _event: self._set_hover(False))
        self.bind("<ButtonPress-1>", lambda _event: self._set_press())
        self.bind("<ButtonRelease-1>", lambda _event: self._set_release())

    def _render_element(self, fill_color: Optional[str] = None) -> None:
        self.delete("all")
        color = fill_color or (self.base_color if not self.is_disabled else "#222530")
        text_color = "#000000" if not self.is_disabled else "#555761"
        pad_w = (self.w * (1.0 - self.current_scale)) / 2
        pad_h = (self.h * (1.0 - self.current_scale)) / 2
        self.create_rectangle(
            pad_w, pad_h, self.w - pad_w, self.h - pad_h, fill=color, outline=""
        )
        self.create_text(
            self.w / 2,
            self.h / 2,
            text=self.text_str,
            fill=text_color,
            font=("Roboto", 11, "bold"),
        )

    def _physics_tick(self) -> None:
        displacement = self.target_scale - self.current_scale
        self.velocity += (displacement * self.stiffness) - (
            self.velocity * (1.0 - self.damping)
        )
        self.current_scale += self.velocity
        if abs(self.velocity) > 0.005 or abs(displacement) > 0.005:
            self._render_element()
            self._anim_id = self.after(16, self._physics_tick)
        else:
            self.current_scale = self.target_scale
            self.velocity = 0.0
            self._render_element()
            self._anim_id = None

    def _start_animation(self) -> None:
        if self._anim_id is not None:
            try:
                self.after_cancel(self._anim_id)
            except Exception:
                pass
        self._physics_tick()

    def _set_hover(self, state: bool) -> None:
        if self.is_disabled:
            return
        self.target_scale = 1.08 if state else 1.0
        self._render_element(self.hover_color if state else self.base_color)
        self._start_animation()

    def _set_press(self) -> None:
        if self.is_disabled:
            return
        self.target_scale = 0.84
        self.velocity = -0.20
        self._start_animation()

    def _set_release(self) -> None:
        if self.is_disabled:
            return
        self.target_scale = 1.0
        self._start_animation()
        if self.command:
            self.command()

    def configure_state(self, state: str, bg_color: Optional[str] = None) -> None:
        self.is_disabled = state == "disabled"
        self.target_scale, self.velocity = 1.0, 0.0
        if bg_color:
            self.base_color = bg_color
        self._render_element()


class DeepNoiseProcessor:
    """Advanced noise reduction and audio enhancement processor"""
    
    def __init__(self, sample_rate: int, channels: int, chunk_size: int):
        self.sr = sample_rate
        self.channels = channels
        self.chunk_size = chunk_size
        
        # Noise gate threshold and attack/release times
        self.noise_gate_threshold = 0.008
        self.gate_attack = 0.01
        self.gate_release = 0.15
        self.gain = 1.0
        
        # Adaptive noise reduction
        self.noise_profile = np.zeros((chunk_size, channels), dtype=np.float32)
        self.noise_alpha = 0.99
        self.spectral_gate_threshold = 2.0
        
        # High-quality EQ for "heaven" mode
        self._init_eq_filters()
        
        # Smooth transition buffer
        self.crossfade_buffer = deque(maxlen=4)
        self.prev_gain = 1.0
        
        # Voice enhancement
        self.voice_clarity_boost = True
        self.compression_ratio = 4.0
        self.compression_threshold = 0.5
        self.last_output_sample = np.zeros(channels, dtype=np.float32)
        self.has_output_sample = False
        
    def _init_eq_filters(self):
        """Initialize high-quality EQ filters for premium audio enhancement"""
        # Low cut filter (remove rumble)
        self.sos_lowcut = signal.butter(4, 80, 'high', fs=self.sr, output='sos')
        
        # Presence boost for clarity (2-5kHz)
        self.sos_presence = signal.butter(2, [2000, 5000], 'bp', fs=self.sr, output='sos')
        
        # Air/high-end boost (10kHz+)
        self.sos_air = signal.butter(2, 10000, 'high', fs=self.sr, output='sos')
        
        # Low-mid cut to reduce mud (200-400Hz)
        self.sos_mudcut = signal.butter(2, [200, 400], 'bp', fs=self.sr, output='sos')
        
        # State variables for filters
        self.zi_lowcut = np.zeros((self.sos_lowcut.shape[0], 2, self.channels))
        self.zi_presence = np.zeros((self.sos_presence.shape[0], 2, self.channels))
        self.zi_air = np.zeros((self.sos_air.shape[0], 2, self.channels))
        self.zi_mudcut = np.zeros((self.sos_mudcut.shape[0], 2, self.channels))
        
    def _spectral_gate(self, data: np.ndarray) -> np.ndarray:
        """Spectral noise gating - removes noise in frequency domain"""
        result = np.zeros_like(data)
        
        for ch in range(self.channels):
            # FFT
            fft_data = rfft(data[:, ch])
            magnitude = np.abs(fft_data)
            phase = np.angle(fft_data)
            
            # Update noise profile (adaptive)
            self.noise_profile[:len(magnitude), ch] = (
                self.noise_alpha * self.noise_profile[:len(magnitude), ch] +
                (1 - self.noise_alpha) * magnitude
            )
            
            # Spectral subtraction with gating
            clean_mag = np.maximum(
                magnitude - self.spectral_gate_threshold * self.noise_profile[:len(magnitude), ch],
                magnitude * 0.1  # Don't completely silence
            )
            
            # Soft gate for smooth transitions
            mask = magnitude > (self.noise_gate_threshold * np.max(magnitude))
            clean_mag = clean_mag * mask + magnitude * (1 - mask) * 0.05
            
            # Reconstruct
            clean_fft = clean_mag * np.exp(1j * phase)
            result[:, ch] = irfft(clean_fft, n=self.chunk_size)
            
        return result
    
    def _apply_eq_heaven_mode(self, data: np.ndarray) -> np.ndarray:
        """Apply premium EQ to make normal speakers sound like high-end USB audio"""
        result = data.copy()
        
        # Apply low cut (remove rumble)
        for i in range(self.channels):
            result[:, i], self.zi_lowcut[:, :, i] = signal.sosfilt(
                self.sos_lowcut, result[:, i], zi=self.zi_lowcut[:, :, i]
            )
        
        # Reduce mud (200-400Hz)
        mud_reduced = np.zeros_like(result)
        for i in range(self.channels):
            mud_reduced[:, i], self.zi_mudcut[:, :, i] = signal.sosfilt(
                self.sos_mudcut, result[:, i], zi=self.zi_mudcut[:, :, i]
            )
        result = result - mud_reduced * 0.4  # Reduce mud by 40%
        
        # Boost presence (2-5kHz) - adds clarity
        presence = np.zeros_like(result)
        for i in range(self.channels):
            presence[:, i], self.zi_presence[:, :, i] = signal.sosfilt(
                self.sos_presence, result[:, i], zi=self.zi_presence[:, :, i]
            )
        result = result + presence * 1.5  # +6dB boost
        
        # Add air/high-end sparkle (10kHz+)
        air = np.zeros_like(result)
        for i in range(self.channels):
            air[:, i], self.zi_air[:, :, i] = signal.sosfilt(
                self.sos_air, result[:, i], zi=self.zi_air[:, :, i]
            )
        result = result + air * 0.8  # +4dB boost
        
        return result
    
    def _apply_compression(self, data: np.ndarray) -> np.ndarray:
        """Dynamic range compression for consistent levels"""
        result = data.copy()
        
        # Calculate RMS level
        rms = np.sqrt(np.mean(data ** 2))
        
        if rms > self.compression_threshold:
            # Apply compression
            gain_reduction = (rms / self.compression_threshold) ** (1 / self.compression_ratio - 1)
            result = result * gain_reduction
            
        return result
    
    def _noise_gate(self, data: np.ndarray) -> np.ndarray:
        """Apply noise gate to silence low-level noise"""
        rms = np.sqrt(np.mean(data ** 2, axis=0))
        
        # Calculate gate gain for each channel
        gate_gain = np.ones(self.channels)
        for ch in range(self.channels):
            if rms[ch] < self.noise_gate_threshold:
                # Attack/Release smoothing
                target_gain = max(0.0, (rms[ch] / self.noise_gate_threshold) ** 2)
                if target_gain < self.prev_gain:
                    # Attack phase
                    gate_gain[ch] = self.prev_gain * (1 - self.gate_attack) + target_gain * self.gate_attack
                else:
                    # Release phase
                    gate_gain[ch] = self.prev_gain * (1 - self.gate_release) + target_gain * self.gate_release
            
        self.prev_gain = np.mean(gate_gain)
        return data * gate_gain
    
    def _smooth_transition(self, data: np.ndarray, is_device_switch: bool = False) -> np.ndarray:
        """Apply smooth crossfade to prevent clicking during device switches"""
        if len(self.crossfade_buffer) < 2:
            self.crossfade_buffer.append(data.copy())
            return data
        
        # Crossfade with previous buffer
        prev_data = self.crossfade_buffer[-2]
        fade_in = np.linspace(0, 1, self.chunk_size).reshape(-1, 1)
        
        # Ensure same shape
        min_len = min(len(prev_data), len(data))
        result = prev_data[:min_len] * (1 - fade_in[:min_len]) + data[:min_len] * fade_in[:min_len]
        
        # Pad if needed
        if len(data) > min_len:
            result = np.vstack([result, data[min_len:]])
            
        self.crossfade_buffer.append(data.copy())
        return result
    
    def process(self, data: np.ndarray, is_system_audio: bool = False, 
                device_just_switched: bool = False) -> np.ndarray:
        """Apply stable, low-cost live processing without musical-noise artifacts."""
        # Ensure correct shape
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        if data.shape[1] != self.channels:
            if data.shape[1] == 1 and self.channels == 2:
                data = np.repeat(data, 2, axis=1)
            else:
                data = data[:, :self.channels]
        
        data = np.asarray(data, dtype=np.float32)
        np.nan_to_num(data, copy=False, nan=0.0, posinf=0.0, neginf=0.0)

        # Preserve system audio exactly. For microphones, only remove low-frequency
        # rumble; spectral subtraction and aggressive gates create audible chirps.
        if not is_system_audio:
            for channel in range(self.channels):
                data[:, channel], self.zi_lowcut[:, :, channel] = signal.sosfilt(
                    self.sos_lowcut,
                    data[:, channel],
                    zi=self.zi_lowcut[:, :, channel],
                )

        # Remove any step between blocks. Even small unreported timing gaps can
        # otherwise become a sharp click after encoding.
        if self.has_output_sample and len(data):
            fade_seconds = 0.01 if device_just_switched else AUDIO_BOUNDARY_FADE_SECONDS
            fade_length = min(len(data), max(1, int(self.sr * fade_seconds)))
            correction = self.last_output_sample - data[0]
            decay = np.linspace(1.0, 0.0, fade_length, dtype=np.float32)[:, None]
            data[:fade_length] += correction[None, :] * decay

        # Scale peaks instead of hard-clipping them, which avoids crackling.
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        if peak > 0.98:
            data *= 0.98 / peak
        if len(data):
            self.last_output_sample = data[-1].copy()
            self.has_output_sample = True
        
        return data


class CaptureEngine:
    def __init__(self, sample_rate: int, channels: int, chunk_size: int, target_fps: float):
        self.sr = sample_rate
        self.channels = channels
        self.chunk_size = chunk_size
        self.fps = target_fps
        self.stop_signal = threading.Event()
        self.sys_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=32)
        self.mic_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=32)
        self.audio_threads: list[threading.Thread] = []
        self.feeder_thread: Optional[threading.Thread] = None
        self.ffmpeg_proc: Optional[subprocess.Popen[bytes]] = None
        self.stderr_file: Optional[Any] = None
        self.output_path: Optional[Path] = None
        self.failure_reason = ""
        self.audio_enabled = False
        self._master_mix = np.zeros((chunk_size, channels), dtype=np.float32)
        self._silence = np.zeros((chunk_size, channels), dtype=np.float32)
        self._sys_pipe_last_sample = np.zeros(channels, dtype=np.float32)
        self._mic_pipe_last_sample = np.zeros(channels, dtype=np.float32)
        self._state_lock = threading.Lock()
        
        # Initialize noise processors for system and mic
        self.sys_processor = DeepNoiseProcessor(sample_rate, channels, chunk_size)
        self.mic_processor = DeepNoiseProcessor(sample_rate, channels, chunk_size)
        
        # Device switching detection
        self._device_switch_lock = threading.Lock()
        self._sys_device_switched = False
        self._mic_device_switched = False

    @property
    def recording(self) -> bool:
        return self.ffmpeg_proc is not None and self.ffmpeg_proc.poll() is None

    @staticmethod
    def _clear_queue(target_queue: queue.Queue[np.ndarray]) -> None:
        while True:
            try:
                target_queue.get_nowait()
            except queue.Empty:
                return

    def _put_latest(self, target_queue: queue.Queue[np.ndarray], chunk: np.ndarray) -> None:
        try:
            target_queue.put(chunk, timeout=0.01)
        except queue.Full:
            try:
                target_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                target_queue.put_nowait(chunk)
            except queue.Full:
                pass

    def _join_pipe_chunk(self, chunk: np.ndarray, previous_sample: np.ndarray) -> np.ndarray:
        """Join a chunk to the pipe stream without a sample-value step."""
        joined = np.asarray(chunk, dtype=np.float32).copy()
        if not len(joined):
            return self._silence.copy()
        fade_length = min(
            len(joined),
            max(1, int(self.sr * AUDIO_BOUNDARY_FADE_SECONDS)),
        )
        correction = previous_sample - joined[0]
        decay = np.linspace(1.0, 0.0, fade_length, dtype=np.float32)[:, None]
        joined[:fade_length] += correction[None, :] * decay
        return joined

    def _conceal_missing_chunk(self, previous_sample: np.ndarray) -> np.ndarray:
        """Fade a late audio source to silence instead of inserting a hard edge."""
        concealed = self._silence.copy()
        fade_length = min(
            len(concealed),
            max(1, int(self.sr * AUDIO_MISSING_FADE_SECONDS)),
        )
        decay = np.linspace(1.0, 0.0, fade_length, dtype=np.float32)[:, None]
        concealed[:fade_length] = previous_sample[None, :] * decay
        return concealed

    @staticmethod
    def _default_speaker_name() -> str:
        speaker = sc.default_speaker()
        return speaker.name if speaker else ""

    @staticmethod
    def _default_microphone_name() -> str:
        microphone = sc.default_microphone()
        return microphone.name if microphone else ""

    def _open_device(self, target_type: str) -> tuple[Any, str]:
        if target_type == "sys":
            default_name = self._default_speaker_name()
            if not default_name:
                raise RuntimeError("No default speaker is available")
            return sc.get_microphone(id=default_name, include_loopback=True), default_name

        default_name = self._default_microphone_name()
        if not default_name:
            raise RuntimeError("No default microphone is available")
        return sc.default_microphone(), default_name

    def _hardware_worker(self, target_type: str) -> None:
        target_queue = self.sys_queue if target_type == "sys" else self.mic_queue
        processor = self.sys_processor if target_type == "sys" else self.mic_processor
        name_reader = (
            self._default_speaker_name
            if target_type == "sys"
            else self._default_microphone_name
        )
        
        # Reset processor for clean start
        processor.__init__(self.sr, self.channels, self.chunk_size)
        discontinuity_count = 0
        last_discontinuity_log = time.monotonic()

        while not self.stop_signal.is_set():
            try:
                device, initial_default_name = self._open_device(target_type)
                write_log(f"Connected {target_type} audio device: {initial_default_name}")
                last_device_check = time.monotonic()
                device_switched = False
                
                with device.recorder(
                    samplerate=self.sr,
                    channels=self.channels,
                    blocksize=self.chunk_size * AUDIO_CAPTURE_BUFFER_MULTIPLIER,
                ) as recorder, warnings.catch_warnings(record=True) as caught_warnings:
                    warnings.simplefilter("always", sc.SoundcardRuntimeWarning)
                    while not self.stop_signal.is_set():
                        caught_warnings.clear()
                        data = recorder.record(numframes=self.chunk_size)

                        discontinuity = any(
                            issubclass(warning.category, sc.SoundcardRuntimeWarning)
                            for warning in caught_warnings
                        )
                        if discontinuity:
                            discontinuity_count += 1
                            now = time.monotonic()
                            if now - last_discontinuity_log >= AUDIO_DISCONTINUITY_LOG_INTERVAL:
                                write_log(
                                    f"{target_type} audio timing glitches: "
                                    f"{discontinuity_count} in the last "
                                    f"{AUDIO_DISCONTINUITY_LOG_INTERVAL:.0f}s"
                                )
                                discontinuity_count = 0
                                last_discontinuity_log = now

                        # Keep the capture thread fast so Windows audio buffers are
                        # drained on time. Only smooth actual timing glitches.
                        data = processor.process(
                            data,
                            is_system_audio=(target_type == "sys"),
                            device_just_switched=(device_switched or discontinuity),
                        )
                        device_switched = False
                        
                        if data.ndim == 1:
                            data = data[:, None]
                        if data.shape[1] == 1 and self.channels == 2:
                            data = np.repeat(data, 2, axis=1)
                        data = np.asarray(data[:, : self.channels], dtype=np.float32)
                        
                        self._put_latest(target_queue, data)

                        now = time.monotonic()
                        if now - last_device_check >= 0.75:
                            current_default_name = name_reader()
                            if current_default_name != initial_default_name:
                                write_log(
                                    f"{target_type} default device changed: "
                                    f"{initial_default_name} -> {current_default_name}"
                                )
                                # Mark device switch for smooth transition
                                device_switched = True
                                break
                            last_device_check = now
                            
            except Exception as exc:
                write_log(f"{target_type} audio reconnecting after error: {exc}")
                self.stop_signal.wait(0.25)

    def _pipe_feeder_worker(self, system_enabled: bool, microphone_enabled: bool) -> None:
        assert self.ffmpeg_proc is not None and self.ffmpeg_proc.stdin is not None
        try:
            block_duration = self.chunk_size / self.sr
            next_deadline = time.monotonic() + block_duration
            
            # Fade in at start
            fade_in_samples = int(self.sr * 0.1)  # 100ms fade in
            samples_faded = 0
            
            while not self.stop_signal.is_set() and self.ffmpeg_proc.poll() is None:
                system_chunk: Optional[np.ndarray] = None
                microphone_chunk: Optional[np.ndarray] = None

                if system_enabled:
                    try:
                        system_chunk = self.sys_queue.get(
                            timeout=max(0.0, next_deadline - time.monotonic())
                        )
                    except queue.Empty:
                        pass
                if microphone_enabled:
                    try:
                        microphone_chunk = self.mic_queue.get(
                            timeout=max(0.0, next_deadline - time.monotonic())
                        )
                    except queue.Empty:
                        pass

                if system_enabled:
                    if system_chunk is None:
                        system_chunk = self._conceal_missing_chunk(self._sys_pipe_last_sample)
                    else:
                        system_chunk = self._join_pipe_chunk(
                            system_chunk,
                            self._sys_pipe_last_sample,
                        )
                    self._sys_pipe_last_sample = system_chunk[-1].copy()
                else:
                    system_chunk = self._silence

                if microphone_enabled:
                    if microphone_chunk is None:
                        microphone_chunk = self._conceal_missing_chunk(self._mic_pipe_last_sample)
                    else:
                        microphone_chunk = self._join_pipe_chunk(
                            microphone_chunk,
                            self._mic_pipe_last_sample,
                        )
                    self._mic_pipe_last_sample = microphone_chunk[-1].copy()
                else:
                    microphone_chunk = self._silence

                remaining = next_deadline - time.monotonic()
                if remaining > 0:
                    self.stop_signal.wait(remaining)
                elif remaining < -(block_duration * 4):
                    next_deadline = time.monotonic()

                self._master_mix.fill(0.0)
                if system_enabled:
                    self._master_mix += system_chunk * (0.68 if microphone_enabled else 1.0)
                if microphone_enabled:
                    self._master_mix += microphone_chunk * (0.68 if system_enabled else 1.0)
                
                # Apply startup fade-in
                if samples_faded < fade_in_samples:
                    fade_gain = min(1.0, samples_faded / fade_in_samples)
                    self._master_mix *= fade_gain
                    samples_faded += self.chunk_size
                
                # A smooth sample-wise limiter avoids block-level gain jumps.
                mixed_peak = float(np.max(np.abs(self._master_mix)))
                if mixed_peak > 0.98:
                    self._master_mix[:] = np.tanh(self._master_mix)
                
                try:
                    self.ffmpeg_proc.stdin.write(self._master_mix.tobytes())
                except (BrokenPipeError, OSError):
                    raise
                    
                next_deadline += block_duration
                
        except (BrokenPipeError, OSError) as exc:
            self.failure_reason = f"FFmpeg audio pipe failed: {exc}"
            write_log(self.failure_reason)
        except Exception:
            self.failure_reason = "Unexpected audio feeder failure"
            log_exception(self.failure_reason)
        finally:
            try:
                # Fade out before closing
                fade_out = np.linspace(1.0, 0.0, self.chunk_size).reshape(-1, 1)
                final_mix = self._master_mix * fade_out
                try:
                    self.ffmpeg_proc.stdin.write(final_mix.astype(np.float32).tobytes())
                except:
                    pass
                self.ffmpeg_proc.stdin.close()
            except Exception:
                pass

    def start(
        self,
        output_path: Path,
        capture_mouse: bool,
        system_audio: bool,
        microphone_audio: bool,
    ) -> None:
        with self._state_lock:
            if self.recording:
                raise RuntimeError("A recording is already in progress")

            writable, reason = directory_is_writable(output_path.parent)
            if not writable:
                raise PermissionError(f"The selected folder is not writable: {reason}")

            self.stop_signal.clear()
            self.failure_reason = ""
            self.output_path = output_path
            self.audio_threads.clear()
            self._clear_queue(self.sys_queue)
            self._clear_queue(self.mic_queue)
            self._sys_pipe_last_sample.fill(0.0)
            self._mic_pipe_last_sample.fill(0.0)
            
            # Reset processors for fresh start
            self.sys_processor = DeepNoiseProcessor(self.sr, self.channels, self.chunk_size)
            self.mic_processor = DeepNoiseProcessor(self.sr, self.channels, self.chunk_size)

            ffmpeg_bin = resolve_ffmpeg()
            audio_enabled = system_audio or microphone_audio
            self.audio_enabled = audio_enabled
            command = [
                ffmpeg_bin,
                "-y",
                "-thread_queue_size",
                "4096",
                "-analyzeduration",
                "0",
                "-probesize",
                "32",
                "-f",
                "gdigrab",
                "-framerate",
                str(int(self.fps)),
                "-draw_mouse",
                "1" if capture_mouse else "0",
                "-i",
                "desktop",
            ]
            if audio_enabled:
                command.extend(
                    [
                        "-thread_queue_size",
                        "4096",
                        "-analyzeduration",
                        "0",
                        "-probesize",
                        "32",
                        "-f",
                        "f32le",
                        "-ar",
                        str(self.sr),
                        "-ac",
                        str(self.channels),
                        "-i",
                        "pipe:0",
                    ]
                )
            command.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-g",
                    str(max(1, int(self.fps * 2))),
                    "-sc_threshold",
                    "0",
                    "-fps_mode",
                    "cfr",
                ]
            )
            if audio_enabled:
                command.extend(
                    [
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-shortest",
                        "-shortest_buf_duration",
                        str(SHORTEST_BUFFER_DURATION_SECONDS),
                    ]
                )
            command.extend(
                [
                    "-movflags",
                    "+frag_keyframe+empty_moov+default_base_moof",
                    "-frag_duration",
                    str(MP4_FRAGMENT_DURATION_US),
                    "-max_interleave_delta",
                    "1000000",
                    "-flush_packets",
                    "1",
                    str(output_path),
                ]
            )

            ensure_app_directory()
            self.stderr_file = LOG_FILE.open("ab")
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                self.ffmpeg_proc = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=self.stderr_file,
                    creationflags=creation_flags,
                )
            except Exception:
                self._close_log_file()
                self.ffmpeg_proc = None
                raise

            time.sleep(0.15)
            if self.ffmpeg_proc.poll() is not None:
                code = self.ffmpeg_proc.returncode
                self._close_log_file()
                self.ffmpeg_proc = None
                raise RuntimeError(f"FFmpeg failed to start. Exit code: {code}. See {LOG_FILE}")

            for enabled, target in (
                (system_audio, "sys"),
                (microphone_audio, "mic"),
            ):
                if enabled:
                    thread = threading.Thread(
                        target=self._hardware_worker,
                        args=(target,),
                        daemon=True,
                        name=f"{target}-audio-capture",
                    )
                    self.audio_threads.append(thread)
                    thread.start()

            if audio_enabled:
                self.feeder_thread = threading.Thread(
                    target=self._pipe_feeder_worker,
                    args=(system_audio, microphone_audio),
                    daemon=True,
                    name="ffmpeg-audio-feeder",
                )
                self.feeder_thread.start()
            else:
                self.feeder_thread = None

            write_log(f"Recording started: {output_path}")

    def _close_log_file(self) -> None:
        if self.stderr_file is not None:
            try:
                self.stderr_file.flush()
                self.stderr_file.close()
            except Exception:
                pass
            self.stderr_file = None

    def _finalize_recording_file(self, output_path: Path) -> tuple[bool, str]:
        """Convert the recoverable live MP4 into a standard player-friendly MP4."""
        finalized_path = output_path.with_name(f".{output_path.stem}.finalizing.mp4")
        finalized_path.unlink(missing_ok=True)
        try:
            command = [
                resolve_ffmpeg(),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "+genpts",
                "-i",
                str(output_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                "-movflags",
                "+faststart",
                str(finalized_path),
            ]
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                timeout=600.0,
            )
            if completed.returncode != 0 or not finalized_path.exists():
                reason = completed.stderr.decode("utf-8", errors="replace").strip()
                return False, reason or f"FFmpeg remux exited with code {completed.returncode}"
            if finalized_path.stat().st_size < MIN_VALID_FILE_SIZE:
                return False, "Finalized recording is unexpectedly small"
            os.replace(finalized_path, output_path)
            return True, ""
        except Exception as exc:
            return False, str(exc)
        finally:
            finalized_path.unlink(missing_ok=True)

    def stop(self) -> tuple[bool, str]:
        with self._state_lock:
            process = self.ffmpeg_proc
            output_path = self.output_path
            if process is None:
                return False, "No recording process was active."
            self.stop_signal.set()

        for thread in self.audio_threads:
            thread.join(timeout=2.0)
        if self.feeder_thread is not None:
            self.feeder_thread.join(timeout=4.0)

        try:
            if process.stdin is not None and not process.stdin.closed:
                if not self.audio_enabled:
                    process.stdin.write(b"q\n")
                    process.stdin.flush()
                else:
                    process.stdin.close()
        except Exception:
            pass

        try:
            return_code = process.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            write_log("FFmpeg did not finalize in time; terminating it")
            process.terminate()
            try:
                return_code = process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                return_code = process.wait(timeout=5.0)

        self._close_log_file()
        with self._state_lock:
            self.ffmpeg_proc = None
            self.feeder_thread = None
            self.audio_threads.clear()
            self.audio_enabled = False

        if return_code != 0:
            message = f"FFmpeg exited with code {return_code}. See log: {LOG_FILE}"
            write_log(message)
            return False, message
        if self.failure_reason:
            return False, f"{self.failure_reason}. See log: {LOG_FILE}"
        if output_path is None or not output_path.exists():
            return False, "FFmpeg exited but the recording file was not created."
        if output_path.stat().st_size < MIN_VALID_FILE_SIZE:
            return False, "The recording file is too small and is probably invalid."

        finalized, reason = self._finalize_recording_file(output_path)
        if not finalized:
            write_log(
                "Recording was saved continuously but standard MP4 finalization "
                f"failed; keeping recoverable file: {reason}"
            )
            return True, (
                f"Recording saved in recoverable MP4 format: {output_path}. "
                f"Standard finalization failed: {reason}"
            )

        message = f"Recording saved: {output_path}"
        write_log(message)
        return True, message


class StudioCapturePro(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Studio Capture Pro")
        self.geometry("600x480")
        self.resizable(False, False)
        self.configure(fg_color="#14161d")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.settings = load_settings()
        self.save_directory = Path(self.settings["save_directory"]).expanduser()
        writable, _reason = directory_is_writable(self.save_directory)
        if not writable:
            self.save_directory = Path.home() / "Videos"
            directory_is_writable(self.save_directory)

        self.engine = CaptureEngine(48000, 2, 1024, 60.0)
        self.out_path: Optional[Path] = None
        self._browse_process: Optional[subprocess.Popen] = None
        self._browse_result_file: Optional[Path] = None
        self._browse_poll_after_id: Optional[str] = None
        self._browse_active = False
        self._finalizing = False
        self.closing = False
        self._build_ui()
        self.after(500, lambda: WindowsGlassEngine.apply_acrylic_theme(self))

    def _build_ui(self) -> None:
        ctk.CTkLabel(
            self,
            text="STUDIO CAPTURE PRO",
            font=("Roboto", 16, "bold"),
            text_color="#00bcd4",
        ).pack(pady=(20, 5))

        self.ui_status = ctk.CTkFrame(self, fg_color="#1a1d26", corner_radius=6, height=42)
        self.ui_status.pack(fill="x", padx=30, pady=10)
        self.ui_status.pack_propagate(False)
        self.dot = ctk.CTkLabel(
            self.ui_status, text="\u2022", font=("Roboto", 24), text_color="#2ecc71"
        )
        self.dot.pack(side="left", padx=(15, 5))
        self.txt = ctk.CTkLabel(
            self.ui_status, text="STANDBY", font=("Roboto", 11, "bold"), text_color="#a0a5b5"
        )
        self.txt.pack(side="left")

        settings_frame = ctk.CTkFrame(self, fg_color="#1a1d26", corner_radius=8)
        settings_frame.pack(fill="both", expand=True, padx=30, pady=10)
        self.sw_mouse = ctk.CTkSwitch(settings_frame, text="Capture Mouse Cursor", progress_color="#00bcd4")
        self.sw_sys = ctk.CTkSwitch(settings_frame, text="Capture System Audio", progress_color="#00bcd4")
        self.sw_mic = ctk.CTkSwitch(settings_frame, text="Capture Microphone", progress_color="#00bcd4")

        switches = (
            (self.sw_mouse, self.settings["capture_mouse"]),
            (self.sw_sys, self.settings["capture_system_audio"]),
            (self.sw_mic, self.settings["capture_microphone"]),
        )
        for switch, enabled in switches:
            if enabled:
                switch.select()
            else:
                switch.deselect()
            switch.pack(anchor="w", padx=25, pady=(15, 5))

        path_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        path_frame.pack(side="bottom", fill="x", padx=25, pady=15)
        self.btn_browse = ctk.CTkButton(
            path_frame,
            text="BROWSE",
            width=70,
            height=24,
            font=("Roboto", 10, "bold"),
            fg_color="#222530",
            hover_color="#00bcd4",
            command=self._browse_folder,
        )
        self.btn_browse.pack(side="right")
        self.lbl_path = ctk.CTkLabel(
            path_frame,
            text=f"Save to: {self.save_directory}",
            font=("Roboto", 11),
            text_color="#a0a5b5",
            anchor="w",
            wraplength=420,
        )
        self.lbl_path.pack(side="left", fill="x", expand=True, padx=(0, 10))

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill="x", padx=30, pady=(10, 15))
        self.btn_rec = FluidGlassButton(controls, text="START", command=self._start)
        self.btn_rec.pack(side="left", padx=(0, 10))
        self.btn_stop = FluidGlassButton(
            controls, text="STOP", command=self._stop, base_color="#222530", hover_color="#e74c3c"
        )
        self.btn_stop.configure_state("disabled")
        self.btn_stop.pack(side="left")

    def _current_settings(self) -> dict[str, Any]:
        return {
            "save_directory": str(self.save_directory),
            "capture_mouse": bool(self.sw_mouse.get()),
            "capture_system_audio": bool(self.sw_sys.get()),
            "capture_microphone": bool(self.sw_mic.get()),
        }

    def _browse_folder(self) -> None:
        """Open the folder picker outside the main UI process."""
        if self.engine.recording or self._browse_active or (
            self._browse_process is not None and self._browse_process.poll() is None
        ):
            return

        self._browse_active = True
        self.btn_browse.configure(state="disabled")
        try:
            ensure_app_directory()
            result_file = APP_DIR / f".folder_picker_{os.getpid()}_{time.time_ns()}.txt"
            result_file.unlink(missing_ok=True)
            command = folder_picker_command(self.save_directory, result_file)
            self._browse_result_file = result_file
            self._browse_process = subprocess.Popen(
                command,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._schedule_browse_poll(125)
        except Exception:
            log_exception("Unable to open folder browser")
            self._browse_cleanup()

    def _schedule_browse_poll(self, delay_ms: int = 125) -> None:
        """Ensure only one poll callback is queued at a time."""
        if self._browse_poll_after_id is None:
            self._browse_poll_after_id = self.after(delay_ms, self._poll_browse_process)

    def _poll_browse_process(self) -> None:
        """Collect the folder picker result while keeping the main UI responsive."""
        self._browse_poll_after_id = None
        process = self._browse_process
        if process is None:
            self._browse_cleanup()
            return

        return_code = process.poll()
        if return_code is None:
            # Slightly lower poll cadence to avoid unnecessary UI churn while dialog is open.
            self._schedule_browse_poll(200)
            return

        result_file = self._browse_result_file
        self._browse_process = None
        self._browse_result_file = None
        try:
            if return_code == 0 and result_file is not None and result_file.exists():
                folder = result_file.read_text(encoding="utf-8").strip()
                if folder:
                    self._handle_folder_selected(folder)
            elif return_code != 0:
                write_log(f"Folder picker exited with code {return_code}")
        except Exception:
            log_exception("Unable to collect folder browser result")
        finally:
            if result_file is not None:
                result_file.unlink(missing_ok=True)
            self._browse_cleanup()

    def _cancel_browse_process(self) -> None:
        """Stop an open picker when the main application is closing."""
        if self._browse_poll_after_id is not None:
            try:
                self.after_cancel(self._browse_poll_after_id)
            except Exception:
                pass
            self._browse_poll_after_id = None
        process = self._browse_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if self._browse_result_file is not None:
            self._browse_result_file.unlink(missing_ok=True)
        self._browse_process = None
        self._browse_result_file = None
        self._browse_active = False
    
    def _browse_worker(self) -> None:
        """Worker thread for folder selection"""
        result_queue = queue.Queue(maxsize=1)
        
        def show_native_dialog():
            """Run on main thread"""
            try:
                folder = self._win_browse_folder_sync()
                result_queue.put(folder)
            except Exception as e:
                write_log(f"Native dialog error: {e}")
                result_queue.put(None)
        
        # Schedule dialog on main thread
        self.after(10, show_native_dialog)
        
        # Wait for result (blocking this worker thread only, not UI)
        try:
            folder = result_queue.get(timeout=60)
            if folder:
                self.after(0, lambda: self._handle_folder_selected(folder))
        except queue.Empty:
            write_log("Folder dialog timeout")
        except Exception as exc:
            write_log(f"Browse worker error: {exc}")
        finally:
            self.after(0, self._browse_cleanup)
    
    def _win_browse_folder_sync(self) -> Optional[str]:
        """Native Windows folder picker - MUST be called on main thread"""
        try:
            from ctypes import wintypes
            
            # BROWSEINFO structure
            class BROWSEINFO(ctypes.Structure):
                _fields_ = [
                    ("hwndOwner", wintypes.HWND),
                    ("pidlRoot", ctypes.c_void_p),
                    ("pszDisplayName", wintypes.LPWSTR),
                    ("lpszTitle", wintypes.LPCWSTR),
                    ("ulFlags", wintypes.UINT),
                    ("lpfn", ctypes.c_void_p),
                    ("lParam", wintypes.LPARAM),
                    ("iImage", wintypes.INT),
                ]
            
            # Constants
            BIF_RETURNONLYFSDIRS = 0x00000001
            BIF_DONTGOBELOWDOMAIN = 0x00000002
            BIF_NEWDIALOGSTYLE = 0x00000040
            BIF_SHAREABLE = 0x00008000
            
            # SHBrowseForFolderW
            shell32 = ctypes.windll.shell32
            shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFO)]
            shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
            
            # SHGetPathFromIDListW
            shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
            shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
            
            # CoTaskMemFree
            ole32 = ctypes.windll.ole32
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            
            # Get parent window handle - MUST be done on main thread
            try:
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
            except:
                hwnd = None
            
            # Setup browse info
            display_name = ctypes.create_unicode_buffer(260)
            browse_info = BROWSEINFO()
            browse_info.hwndOwner = hwnd
            browse_info.pidlRoot = None
            browse_info.pszDisplayName = display_name
            browse_info.lpszTitle = "Select the folder where recordings will be saved:"
            browse_info.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_SHAREABLE
            browse_info.lpfn = None
            browse_info.lParam = 0
            
            # Show dialog - this will block but native Windows dialog
            # doesn't freeze the app's message pump like tkinter does
            pidl = shell32.SHBrowseForFolderW(ctypes.byref(browse_info))
            
            if not pidl:
                return None
            
            # Get path from PIDL
            path_buffer = ctypes.create_unicode_buffer(260)
            result = None
            if shell32.SHGetPathFromIDListW(pidl, path_buffer):
                result = path_buffer.value
            
            # Free PIDL
            ole32.CoTaskMemFree(pidl)
            
            return result
            
        except Exception as exc:
            write_log(f"Windows browse error: {exc}")
            return None
    
    def _handle_folder_selected(self, folder: str) -> None:
        """Handle folder selection result on main thread"""
        try:
            selected = Path(folder)
            writable, reason = directory_is_writable(selected)
            if not writable:
                messagebox.showerror("Folder Error", f"This folder is not writable:\n{reason}", parent=self)
                return
            self.save_directory = selected
            self.lbl_path.configure(text=f"Save to: {self.save_directory}")
            save_settings(self._current_settings())
        except Exception as exc:
            write_log(f"Handle folder error: {exc}")
    
    def _browse_cleanup(self) -> None:
        """Re-enable browse button after dialog closes"""
        self._browse_active = False
        self._browse_poll_after_id = None
        if not self.engine.recording:
            self.btn_browse.configure(state="normal")
        self.update_idletasks()

    def _set_recording_controls(self, recording: bool) -> None:
        self.btn_rec.configure_state("disabled" if recording else "normal", bg_color="#00bcd4")
        self.btn_stop.configure_state("normal" if recording else "disabled", bg_color="#e74c3c")
        self.btn_browse.configure(state="disabled" if recording else "normal")
        for switch in (self.sw_mouse, self.sw_sys, self.sw_mic):
            switch.configure(state="disabled" if recording else "normal")

    def _start(self) -> None:
        writable, reason = directory_is_writable(self.save_directory)
        if not writable:
            messagebox.showerror("Save Error", f"The selected folder is not writable:\n{reason}", parent=self)
            return

        self.out_path = unique_recording_path(self.save_directory)
        try:
            self.engine.start(
                self.out_path,
                bool(self.sw_mouse.get()),
                bool(self.sw_sys.get()),
                bool(self.sw_mic.get()),
            )
        except Exception as exc:
            log_exception("Recording failed to start")
            messagebox.showerror("Recording Error", f"{exc}\n\nLog: {LOG_FILE}", parent=self)
            return

        save_settings(self._current_settings())
        self.lbl_path.configure(text=f"Recording: {self.out_path.name}")
        self._set_recording_controls(True)
        self.dot.configure(text_color="#e74c3c")
        self.txt.configure(text="LIVE \u2022 60 FPS", text_color="#e74c3c")

    def _stop(self) -> None:
        if self._finalizing or self.engine.ffmpeg_proc is None:
            return
        self._finalizing = True
        self.btn_stop.configure_state("disabled")
        self.txt.configure(text="FINALIZING...", text_color="#3498db")
        threading.Thread(target=self._teardown, daemon=True, name="recording-finalizer").start()

    def _teardown(self) -> None:
        try:
            success, message = self.engine.stop()
        except Exception as exc:
            log_exception("Unexpected recording finalization failure")
            success, message = False, f"Unable to finalize recording: {exc}\n\nLog: {LOG_FILE}"
        self.after(0, lambda: self._reset_ui(success, message))

    def _reset_ui(self, success: bool, message: str) -> None:
        self._finalizing = False
        self._set_recording_controls(False)
        self.dot.configure(text_color="#2ecc71" if success else "#e74c3c")
        self.txt.configure(
            text="RECORDING SAVED" if success else "RECORDING ERROR",
            text_color="#2ecc71" if success else "#e74c3c",
        )
        if success and self.out_path:
            self.lbl_path.configure(text=f"Saved: {self.out_path}")
        if not success:
            messagebox.showerror("Recording Error", message, parent=self)
        if self.closing:
            self._cancel_browse_process()
            self.destroy()

    def _on_close(self) -> None:
        save_settings(self._current_settings())
        if self._finalizing:
            self.closing = True
            return
        if not self.engine.recording:
            self._cancel_browse_process()
            self.destroy()
            return
        if not messagebox.askyesno(
            "Finish Recording",
            "A recording is active. Stop and save it before closing?",
            parent=self,
        ):
            return
        self.closing = True
        self._set_recording_controls(True)
        self._stop()


def main():
    """Package entry point hook for system binaries."""
    ensure_app_directory()
    write_log("Application started via package wrapper")
    app = StudioCapturePro()
    app.mainloop()


if __name__ == "__main__":
    # If users launch the dialog picker worker, handle it directly
    if len(sys.argv) > 1 and sys.argv[1] == FOLDER_PICKER_ARG:
        raise SystemExit(run_folder_picker_helper())
    else:
        main()
