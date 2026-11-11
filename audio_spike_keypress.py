"""Listen to Windows computer audio and send a configurable key on loud spikes."""
from __future__ import annotations

import collections
import json
import math
import os
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np
import soundcard as sc
from pynput import keyboard

SAMPLE_RATE = 48_000
FRAME_SECONDS = 0.02
BASELINE_SECONDS = 3.0


def settings_file() -> Path:
    """Return a per-user, persistent settings path."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "AudioSpikeKeypress" / "settings.json"


def key_name(key) -> str | None:
    """Convert a pynput key event to a short, user-facing name."""
    if isinstance(key, keyboard.KeyCode):
        return key.char.lower() if key.char else None
    names = {
        keyboard.Key.space: "space",
        keyboard.Key.enter: "enter",
        keyboard.Key.tab: "tab",
        keyboard.Key.esc: "esc",
        keyboard.Key.backspace: "backspace",
        keyboard.Key.delete: "delete",
    }
    if key in names:
        return names[key]
    name = str(key).replace("Key.", "")
    if name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 12:
        return name
    return None


def pynput_key(name: str):
    name = name.strip().lower()
    special = {
        "space": keyboard.Key.space,
        "enter": keyboard.Key.enter,
        "tab": keyboard.Key.tab,
        "esc": keyboard.Key.esc,
        "backspace": keyboard.Key.backspace,
        "delete": keyboard.Key.delete,
    }
    if name in special:
        return special[name]
    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)
    if name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 12:
        return getattr(keyboard.Key, name)
    raise ValueError("Use one letter/number, F1–F12, Space, Enter, Tab, Esc, Backspace, or Delete.")


class AudioSpikeKeypressApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Audio Spike Keypress")
        self.root.resizable(False, False)
        self.stop_event = threading.Event()
        self.audio_thread: threading.Thread | None = None
        self.listening = False
        self.capture_target: str | None = None
        self.listener = keyboard.Listener(on_press=self.on_global_key)
        self.listener.daemon = True
        self.listener.start()
        self.controller = keyboard.Controller()
        self.cooldown_until = 0.0
        self.recent_events: collections.deque[float] = collections.deque()
        self.armed = True
        self.quiet_for = 0.0
        self.run_action = "a"
        self.run_settings = {}
        self.last_keypress_at = 0.0
        self.action_lock = threading.Lock()
        self.action_sequence_running = False
        self.timeout_pending = False
        self.last_trigger_at = 0.0

        self.toggle_var = tk.StringVar(value="f8")
        self.action_var = tk.StringVar(value="a")
        self.timeout_key_var = tk.StringVar(value="f9")
        self.repeat_delay_var = tk.StringVar(value="1.0")
        self.timeout_seconds_var = tk.StringVar(value="0")
        self.min_level_var = tk.StringVar(value="-35")
        self.rise_var = tk.StringVar(value="12")
        self.burst_count_var = tk.StringVar(value="4")
        self.burst_window_var = tk.StringVar(value="5")
        self.pause_var = tk.StringVar(value="15")
        self.status_var = tk.StringVar(value="Stopped. Press F8 or click Start listening.")
        self.level_var = tk.StringVar(value="Current level: —")
        self.load_settings()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=14)
        frame.grid(sticky="nsew")
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Audio Spike Keypress", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            frame,
            text="Monitors the default output device (computer sound) using loopback audio.",
            wraplength=430,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self._key_row(frame, 2, "Start/stop key", self.toggle_var, "toggle")
        self._key_row(frame, 3, "Key to press on a spike", self.action_var, "action")
        self._key_row(frame, 4, "Key when no spike is heard", self.timeout_key_var, "timeout")
        self._number_row(frame, 5, "No-spike timeout (seconds; 0 disables)", self.timeout_seconds_var)
        self._number_row(frame, 6, "Minimum wait between keypresses (seconds)", self.repeat_delay_var)
        self._number_row(frame, 7, "Minimum loudness (dBFS)", self.min_level_var)
        self._number_row(frame, 8, "Rise above recent background (dB)", self.rise_var)
        ttk.Separator(frame).grid(row=9, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(frame, text="Burst protection", font=("Segoe UI", 10, "bold")).grid(
            row=10, column=0, columnspan=3, sticky="w", pady=(0, 5)
        )
        self._number_row(frame, 11, "Peak limit within the time window", self.burst_count_var)
        self._number_row(frame, 12, "Time window (seconds)", self.burst_window_var)
        self._number_row(frame, 13, "Pause after the peak limit (seconds)", self.pause_var)

        buttons = ttk.Frame(frame)
        buttons.grid(row=14, column=0, columnspan=3, sticky="ew", pady=(12, 6))
        self.start_button = ttk.Button(buttons, text="Start listening", command=self.toggle_listening)
        self.start_button.pack(side="left")
        ttk.Button(buttons, text="Test key", command=self.test_action_key).pack(side="left", padx=8)
        ttk.Label(frame, textvariable=self.status_var, wraplength=430).grid(
            row=15, column=0, columnspan=3, sticky="w", pady=(2, 4)
        )
        ttk.Label(frame, textvariable=self.level_var).grid(row=16, column=0, columnspan=3, sticky="w")
        ttk.Label(
            frame,
            text="Tip: adjust the dBFS threshold while watching the level. A less negative threshold requires a louder sound.",
            wraplength=430,
            foreground="#555555",
        ).grid(row=17, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _number_row(self, parent, row, label, variable):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=variable, width=11).grid(row=row, column=1, sticky="w", padx=(10, 0), pady=3)

    def _key_row(self, parent, row, label, variable, target):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=variable, width=11, state="readonly").grid(
            row=row, column=1, sticky="w", padx=(10, 0), pady=3
        )
        ttk.Button(parent, text="Choose key", command=lambda: self.capture_key(target)).grid(
            row=row, column=2, sticky="w", padx=(8, 0), pady=3
        )

    def capture_key(self, target: str):
        self.capture_target = target
        self.status_var.set("Press the key you want to assign…")

    def on_global_key(self, key):
        name = key_name(key)
        if not name:
            return
        if self.capture_target:
            target, self.capture_target = self.capture_target, None
            variable = {
                "toggle": self.toggle_var,
                "action": self.action_var,
                "timeout": self.timeout_key_var,
            }[target]
            self.root.after(0, lambda: self._set_key(variable, name))
            return
        if name == self.toggle_var.get().strip().lower():
            self.root.after(0, self.toggle_listening)

    def _set_key(self, variable, name):
        variable.set(name)
        self.status_var.set(f"Assigned {name.upper()}.")

    def _settings(self):
        toggle = self.toggle_var.get().strip().lower()
        action = self.action_var.get().strip().lower()
        timeout_key = self.timeout_key_var.get().strip().lower()
        pynput_key(toggle)
        pynput_key(action)
        pynput_key(timeout_key)
        if toggle in (action, timeout_key):
            raise ValueError("The start/stop key must differ from both action keys.")
        values = {
            "repeat_delay": float(self.repeat_delay_var.get()),
            "timeout_seconds": float(self.timeout_seconds_var.get()),
            "min_level": float(self.min_level_var.get()),
            "rise": float(self.rise_var.get()),
            "burst_count": int(self.burst_count_var.get()),
            "burst_window": float(self.burst_window_var.get()),
            "pause": float(self.pause_var.get()),
        }
        if values["repeat_delay"] < 0 or values["timeout_seconds"] < 0 or values["burst_count"] < 1 or values["burst_window"] <= 0 or values["pause"] < 0:
            raise ValueError("Delays must be non-negative, and burst count/window must be greater than zero.")
        if not -120 <= values["min_level"] <= 0 or not 0 < values["rise"] <= 100:
            raise ValueError("Use a minimum level from -120 to 0 dBFS and a rise from 0 to 100 dB.")
        return toggle, action, timeout_key, values

    def load_settings(self):
        path = settings_file()
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(saved, dict):
            return
        variables = {
            "toggle": self.toggle_var,
            "action": self.action_var,
            "timeout_key": self.timeout_key_var,
            "repeat_delay": self.repeat_delay_var,
            "timeout_seconds": self.timeout_seconds_var,
            "min_level": self.min_level_var,
            "rise": self.rise_var,
            "burst_count": self.burst_count_var,
            "burst_window": self.burst_window_var,
            "pause": self.pause_var,
        }
        for name, variable in variables.items():
            if name in saved:
                variable.set(str(saved[name]))

    def save_settings(self):
        saved = {
            "toggle": self.toggle_var.get(),
            "action": self.action_var.get(),
            "timeout_key": self.timeout_key_var.get(),
            "repeat_delay": self.repeat_delay_var.get(),
            "timeout_seconds": self.timeout_seconds_var.get(),
            "min_level": self.min_level_var.get(),
            "rise": self.rise_var.get(),
            "burst_count": self.burst_count_var.get(),
            "burst_window": self.burst_window_var.get(),
            "pause": self.pause_var.get(),
        }
        path = settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        temporary.replace(path)

    def toggle_listening(self):
        if self.listening:
            self.stop_listening()
        else:
            self.start_listening()

    def start_listening(self):
        try:
            _, self.run_action, self.run_timeout_key, self.run_settings = self._settings()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("Check settings", str(exc))
            return
        self.stop_event.clear()
        self.listening = True
        self.armed = True
        self.quiet_for = 0.0
        self.recent_events.clear()
        self.cooldown_until = 0.0
        self.last_keypress_at = 0.0
        self.action_sequence_running = False
        self.timeout_pending = False
        self.last_trigger_at = time.monotonic()
        try:
            self.save_settings()
        except OSError as exc:
            self.status_var.set(f"Could not save settings: {exc}")
        self.start_button.configure(text="Stop listening")
        try:
            self._press_key(self.run_action)
        except Exception as exc:
            self.status_var.set(f"Listening, but the initial keypress failed: {exc}")
        else:
            self.status_var.set("Initial keypress sent. Listening to computer audio…")
        self.audio_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.audio_thread.start()

    def stop_listening(self):
        self.stop_event.set()
        self.listening = False
        self.start_button.configure(text="Start listening")
        self.status_var.set("Stopped.")

    def _listen_loop(self):
        try:
            speaker = sc.default_speaker()
            if speaker is None:
                raise RuntimeError("No default output device was found.")
            loopback = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            baseline = collections.deque(maxlen=int(BASELINE_SECONDS / FRAME_SECONDS))
            frames = int(SAMPLE_RATE * FRAME_SECONDS)
            while not self.stop_event.is_set():
                audio = loopback.record(samplerate=SAMPLE_RATE, numframes=frames)
                if audio is None or not len(audio):
                    continue
                rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
                level = 20.0 * math.log10(max(rms, 1e-8))
                now = time.monotonic()
                baseline_db = float(np.percentile(baseline, 20)) if baseline else -120.0
                action, settings = self.run_action, self.run_settings
                trigger_db = max(settings["min_level"], baseline_db + settings["rise"])
                rearm_db = max(settings["min_level"] - 5.0, baseline_db + settings["rise"] - 6.0)
                self.root.after(0, lambda lev=level, base=baseline_db, trig=trigger_db:
                                self.level_var.set(f"Current: {lev:.1f} dBFS | Background: {base:.1f} | Trigger: {trig:.1f}"))

                if now >= self.cooldown_until and self.armed and level >= trigger_db:
                    self.armed = False
                    self.quiet_for = 0.0
                    self._record_spike(now, level, action, settings)
                elif not self.armed:
                    if level <= rearm_db:
                        self.quiet_for += FRAME_SECONDS
                        if self.quiet_for >= 0.12:
                            self.armed = True
                    else:
                        self.quiet_for = 0.0

                # Only add the quieter frames to the rolling background estimate.
                if level < trigger_db:
                    baseline.append(level)
                elif not baseline:
                    baseline.append(level)

                timeout_s = settings["timeout_seconds"]
                if timeout_s > 0 and now - self.last_trigger_at >= timeout_s and not self.timeout_pending:
                    self.timeout_pending = True
                    threading.Thread(
                        target=self._press_timeout_key,
                        args=(self.run_timeout_key, timeout_s, settings["repeat_delay"]),
                        daemon=True,
                    ).start()
        except Exception as exc:
            self.root.after(0, lambda e=str(exc): self._audio_failed(e))

    def _record_spike(self, now, level, action, settings):
        self.last_trigger_at = now
        window = settings["burst_window"]
        while self.recent_events and now - self.recent_events[0] > window:
            self.recent_events.popleft()
        self.recent_events.append(now)
        count = settings["burst_count"]
        if len(self.recent_events) >= count:
            self.cooldown_until = now + settings["pause"]
            self.recent_events.clear()
            self.root.after(0, lambda pause=settings["pause"]:
                            self.status_var.set(f"Peak burst detected. Pausing detection for {pause:g} seconds."))
        else:
            self.root.after(0, lambda lev=level: self.status_var.set(f"Spike detected ({lev:.1f} dBFS). Sending key."))
            threading.Thread(target=self._press_twice, args=(action, settings["repeat_delay"]), daemon=True).start()

    def _press_timeout_key(self, action, timeout_s, delay):
        acquired = False
        try:
            while not self.stop_event.is_set():
                acquired = self.action_lock.acquire(timeout=0.1)
                if acquired:
                    break
            if not acquired:
                return
            remaining = max(0.0, self.last_keypress_at + delay - time.monotonic())
            if self.stop_event.wait(remaining):
                return
            # A spike while the timeout key was waiting cancels this timeout.
            if self.stop_event.is_set() or time.monotonic() - self.last_trigger_at < timeout_s:
                return
            self._press_key(action)
            self.last_trigger_at = time.monotonic()
            self.root.after(0, lambda key=action: self.status_var.set(
                f"No spike for {timeout_s:g} seconds. Sent timeout key {key.upper()}."
            ))
        except Exception as exc:
            self.root.after(0, lambda e=str(exc): self.status_var.set(f"Could not send timeout key: {e}"))
        finally:
            if acquired:
                self.action_lock.release()
            self.timeout_pending = False

    def _press_twice(self, action, delay):
        # One sequence at a time; every keypress observes the same minimum gap.
        if not self.action_lock.acquire(blocking=False):
            return
        self.action_sequence_running = True
        try:
            for _ in range(2):
                remaining = max(0.0, self.last_keypress_at + delay - time.monotonic())
                if self.stop_event.wait(remaining):
                    return
                self._press_key(action)
        except Exception as exc:
            self.root.after(0, lambda e=str(exc): self.status_var.set(f"Could not send key: {e}"))
        finally:
            self.action_sequence_running = False
            self.action_lock.release()

    def _press_key(self, action):
        key = pynput_key(action)
        self.controller.press(key)
        self.controller.release(key)
        self.last_keypress_at = time.monotonic()

    def test_action_key(self):
        try:
            _, action, _, _ = self._settings()
            self._press_key(action)
            self.status_var.set(f"Sent test key {action.upper()}.")
        except Exception as exc:
            messagebox.showerror("Could not send key", str(exc))

    def _audio_failed(self, error):
        self.stop_listening()
        messagebox.showerror(
            "Audio capture failed",
            "Could not capture computer audio. Check that the default output device is available and that loopback capture is supported.\n\n" + error,
        )

    def close(self):
        self.stop_event.set()
        self.listening = False
        try:
            self.save_settings()
        except OSError as exc:
            messagebox.showwarning("Settings not saved", f"Could not save settings:\n{exc}")
        try:
            self.listener.stop()
        finally:
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = AudioSpikeKeypressApp(root)
    root.mainloop()
