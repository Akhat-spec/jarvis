"""Потоковая озвучка.

Ответ режется на фразы: следующая синтезируется, пока играет текущая.
ElevenLabs отдаёт «сырой» PCM, который играется напрямую через звуковую карту, поэтому
Джарвис в каждый момент знает громкость своего голоса (опорный сигнал) — слух использует
его, чтобы отличать эхо из колонок от голоса пользователя (перебивание, этап 2).
Edge-голос (запасной) играется как mp3 через Windows MCI.
"""
import asyncio
import collections
import ctypes
import json
import os
import queue
import tempfile
import threading
import time
import urllib.error
import urllib.request

import edge_tts
import numpy as np
import sounddevice as sd

from .config import (ELEVENLABS_API_KEY, ELEVENLABS_MODEL, ELEVENLABS_SIMILARITY, ELEVENLABS_SPEED,
                     ELEVENLABS_STABILITY, ELEVENLABS_STYLE, ELEVENLABS_VOICE_ID, TTS_PROVIDER, VOICE,
                     VOICE_RATE)

PCM_RATE = 22050
HOLD_MAX_SEC = 15         # самая длинная пауза «прислушивания» (фраза до 15 с + запас)
ELEVENLABS_RETRY_SEC = 300  # после сбоя ElevenLabs через 5 минут снова пробуем основной голос
BLOCK = 1024  # ~46 мс — шаг проверки «стоп» и обновления опорного уровня


class _PcmStream:
    """Фраза, которая скачивается из ElevenLabs в фоне и играется по мере поступления."""

    def __init__(self, text: str):
        self.chunks: queue.Queue = queue.Queue()
        self.error: Exception | None = None
        self.first_chunk = threading.Event()
        threading.Thread(target=self._fetch, args=(text,), daemon=True, name="tts-fetch").start()

    def _fetch(self, text: str) -> None:
        try:
            with _open_elevenlabs(text) as resp:
                tail = b""
                while True:
                    data = resp.read(4096)
                    if not data:
                        break
                    data = tail + data
                    cut = len(data) // 2 * 2
                    tail = data[cut:]
                    self.chunks.put(np.frombuffer(data[:cut], dtype=np.int16))
                    self.first_chunk.set()
        except Exception as e:
            self.error = e
        finally:
            self.first_chunk.set()
            self.chunks.put(None)


def _open_elevenlabs(text: str):
    """Открывает потоковый запрос к ElevenLabs (голос George) → int16 PCM 22 кГц."""
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
           f"?output_format=pcm_{PCM_RATE}")
    body = json.dumps({
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "language_code": "ru",
        "voice_settings": {
            "stability": ELEVENLABS_STABILITY, "similarity_boost": ELEVENLABS_SIMILARITY,
            "style": ELEVENLABS_STYLE, "speed": ELEVENLABS_SPEED, "use_speaker_boost": True,
        },
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json",
    })
    try:
        return urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        raise RuntimeError(f"ElevenLabs {e.code}: {detail}") from None


def _synth_elevenlabs(text: str) -> np.ndarray:
    """Целиком (для тестов)."""
    with _open_elevenlabs(text) as resp:
        raw = resp.read()
    return np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16)


def _synth_edge(text: str, attempts: int = 3) -> str:
    """Сервис Edge иногда отвечает «No audio was received» — повторяем, а не теряем фразу."""
    fd, path = tempfile.mkstemp(suffix=".mp3", prefix="jarvis_")
    os.close(fd)
    for i in range(attempts):
        try:
            asyncio.run(edge_tts.Communicate(text, VOICE, rate=VOICE_RATE).save(path))
            return path
        except Exception:
            if i == attempts - 1:
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise
            time.sleep(0.4 * (i + 1))
    return path


class Speaker:
    def __init__(self, on_start=None, on_error=None):
        self._texts: queue.Queue = queue.Queue()
        self._items: queue.Queue = queue.Queue()
        self._gen = 0                 # «поколение»: stop() делает все старые фразы устаревшими
        self._pending = 0
        self._lock = threading.Lock()
        self._stop_now = threading.Event()
        self._paused = threading.Event()   # пауза на время проверки «это речь или шум?»
        self.on_start = on_start      # колбэк(текст) — фраза начала звучать
        self.on_error = on_error
        self.provider = TTS_PROVIDER if TTS_PROVIDER in ("elevenlabs", "edge") else "edge"
        self._el_failed_at = 0.0

        # для слуха: что и насколько громко Джарвис говорит прямо сейчас
        self.ref_level = 0.0          # средняя амплитуда текущего блока (int16), 0 — тишина
        self.ref_known = False        # True, пока играет PCM (уровень известен точно)
        self.playing = False
        self.recent = collections.deque(maxlen=6)   # (время, текст) последних фраз
        self.last_synth_ms = 0

        threading.Thread(target=self._synth_loop, daemon=True, name="tts-synth").start()
        threading.Thread(target=self._play_loop, daemon=True, name="tts-play").start()

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def say(self, text: str) -> None:
        if getattr(self, "muted", False):  # «Озвучивать ответы» выключено в настройках HUD
            return
        text = text.strip()
        if not any(ch.isalnum() for ch in text):  # «…», «—» и т.п. озвучивать нечего
            return
        with self._lock:
            self._pending += 1
            gen = self._gen
        self._texts.put((gen, text))

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
        self._paused.clear()
        self._stop_now.set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def recent_text(self, seconds: float = 12) -> str:
        now = time.time()
        return " ".join(t for ts, t in self.recent if now - ts < seconds)

    # ── внутреннее ──
    def _done(self) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)

    def _synth_loop(self) -> None:
        while True:
            gen, text = self._texts.get()
            if gen != self._gen:
                self._done()
                continue
            item = None
            t0 = time.time()
            try:
                if (self.provider == "edge" and TTS_PROVIDER == "elevenlabs"
                        and time.time() - self._el_failed_at > ELEVENLABS_RETRY_SEC):
                    self.provider = "elevenlabs"  # временный сбой сети не должен навсегда менять голос
                if self.provider == "elevenlabs":
                    stream = _PcmStream(text)        # качается в фоне, пока играет предыдущая фраза
                    stream.first_chunk.wait(20)
                    if stream.error is None:
                        item = ("stream", stream)
                    else:  # кончились символы, неверный ключ, нет сети
                        if self.on_error:
                            self.on_error(f"{stream.error}. Временно переключаюсь на голос Edge.")
                        self.provider = "edge"
                        self._el_failed_at = time.time()
                if item is None:
                    item = ("mp3", _synth_edge(text))
            except Exception as e:
                if self.on_error:
                    self.on_error(f"Озвучка недоступна: {e}")
            self.last_synth_ms = int((time.time() - t0) * 1000)
            self._items.put((gen, item, text))

    def _play_loop(self) -> None:
        while True:
            gen, item, text = self._items.get()
            try:
                if item is None or gen != self._gen:
                    continue
                self._stop_now.clear()
                self.recent.append((time.time(), text))
                if self.on_start:
                    self.on_start(text)
                self.playing = True
                if item[0] == "stream":
                    self._play_stream(item[1], gen)
                elif item[0] == "pcm":
                    self._play_pcm(item[1], gen)
                else:
                    self._play_mp3(item[1], gen)
            except Exception as e:
                if self.on_error:
                    self.on_error(f"Ошибка воспроизведения: {e}")
            finally:
                self.playing = False
                self.ref_level, self.ref_known = 0.0, False
                if item and item[0] == "mp3":
                    try:
                        os.remove(item[1])
                    except OSError:
                        pass
                self._done()

    def _interrupted(self, gen: int) -> bool:
        return self._stop_now.is_set() or gen != self._gen

    def _hold(self, gen: int) -> bool:
        """Ждёт, пока снята пауза. False — если за это время фразу отменили.
        Страховка: пауза дольше HOLD_MAX_SEC снимается сама — Джарвис не может «замереть» навсегда."""
        started = time.time()
        while self._paused.is_set():
            self.ref_level = 0.0
            if self._interrupted(gen):
                return False
            if time.time() - started > HOLD_MAX_SEC:
                self._paused.clear()
                break
            time.sleep(0.02)
        return not self._interrupted(gen)

    def _play_pcm(self, pcm: np.ndarray, gen: int) -> None:
        self.ref_known = True
        with sd.OutputStream(samplerate=PCM_RATE, channels=1, dtype="int16", blocksize=BLOCK) as out:
            for i in range(0, len(pcm), BLOCK):
                if not self._hold(gen) or self._interrupted(gen):
                    out.abort()
                    return
                block = pcm[i:i + BLOCK]
                self.ref_level = float(np.abs(block).mean()) if len(block) else 0.0
                out.write(block.reshape(-1, 1))

    def _play_stream(self, stream: "_PcmStream", gen: int) -> None:
        self.ref_known = True
        pending = np.zeros(0, dtype=np.int16)
        with sd.OutputStream(samplerate=PCM_RATE, channels=1, dtype="int16", blocksize=BLOCK) as out:
            done = False
            while not done or len(pending):
                if self._interrupted(gen):
                    out.abort()
                    return
                if not done and len(pending) < BLOCK:
                    try:
                        chunk = stream.chunks.get(timeout=0.05 if len(pending) else 5)
                    except queue.Empty:
                        chunk = np.zeros(0, dtype=np.int16) if len(pending) else None
                    if chunk is None:
                        done = True
                    else:
                        pending = np.concatenate([pending, chunk])
                    continue
                if not self._hold(gen):
                    out.abort()
                    return
                block, pending = pending[:BLOCK], pending[BLOCK:]
                self.ref_level = float(np.abs(block).mean()) if len(block) else 0.0
                out.write(block.reshape(-1, 1))

    def _play_mp3(self, path: str, gen: int) -> None:
        mci = ctypes.windll.winmm.mciSendStringW
        buf = ctypes.create_unicode_buffer(64)
        if mci(f'open "{path}" type mpegvideo alias jv', None, 0, None) != 0:
            return
        mci("play jv", None, 0, None)
        while True:
            time.sleep(0.04)
            if self._interrupted(gen):
                break
            if self._paused.is_set():
                mci("pause jv", None, 0, None)
                if not self._hold(gen):
                    break
                mci("resume jv", None, 0, None)
            mci("status jv mode", buf, 64, None)
            if buf.value not in ("playing", "seeking", "not ready"):
                break
        mci("stop jv", None, 0, None)
        mci("close jv", None, 0, None)
