"""Слух: микрофон → VAD (начало/конец фразы) → распознавание речи.

Этап 2 — живой диалог:
* микрофон слушает всегда, даже пока Джарвис думает или говорит;
* перебивание (barge-in): если пользователь заговорил поверх ответа, ответ обрывается;
* фильтр эха: громкость голоса Джарвиса известна (Speaker.ref_level), поэтому слух
  ожидает «эхо» соответствующей силы и реагирует только на то, что заметно громче;
  распознанный текст дополнительно сверяется с тем, что Джарвис только что сказал;
* живая расшифровка (Vosk, если установлена модель) — HUD видит слова по мере речи.
"""
import collections
import difflib
import os
import re
import threading
import time

from .config import ROOT

SAMPLE_RATE = 16000
CHUNK_SEC = 0.05
END_SILENCE_SEC = 0.7      # пауза, после которой фраза считается законченной
MAX_PHRASE_SEC = 15
MIN_PHRASE_SEC = 0.35
MIN_THRESHOLD = 120        # нижняя граница порога громкости (int16)
BARGE_SUSTAIN_SEC = 0.35   # столько нужно говорить поверх Джарвиса, чтобы он замер и прислушался
ECHO_MARGIN = 3.0          # во сколько раз голос должен быть громче ожидаемого эха
VOSK_MODEL_DIR = ROOT / "models" / "vosk-model-small-ru-0.22"

# Виртуальные «микрофоны», которые часто стоят по умолчанию, но не слышат голос
VIRTUAL_MIC_WORDS = ("voicemod", "переназначение", "sound mapper", "стерео микшер", "stereo mix",
                     "cable", "virtual", "первичный драйвер")


def pick_microphones(sd) -> list[int]:
    """Порядок попыток: JARVIS_MIC из .env → микрофон по умолчанию (если не виртуальный) → остальные."""
    devices = sd.query_devices()
    mme = next((i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "MME"), None)
    candidates = [i for i, d in enumerate(devices)
                  if d["max_input_channels"] > 0 and (mme is None or d["hostapi"] == mme)
                  and not any(w in d["name"].lower() for w in VIRTUAL_MIC_WORDS)]

    wanted = os.getenv("JARVIS_MIC", "").strip()
    if wanted:
        if wanted.isdigit():
            return [int(wanted)]
        match = [i for i in candidates if wanted.lower() in devices[i]["name"].lower()]
        if match:
            return match

    default_in = sd.default.device[0]
    order = [default_in] if default_in in candidates else []
    # обычный «Микрофон» раньше «Набора микрофонов» — массив часто отключён драйвером
    order += sorted((i for i in candidates if i not in order),
                    key=lambda i: 0 if devices[i]["name"].lower().startswith("микрофон") else 1)
    return order or [default_in]


def _normalize(np, audio: bytes) -> bytes:
    """Подтягивает громкость тихой записи (встроенный микрофон ноутбука) — Google распознаёт лучше."""
    pcm = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    pcm -= pcm.mean()
    peak = float(np.abs(pcm).max()) or 1.0
    gain = min(12.0, 26000.0 / peak)
    return np.clip(pcm * gain, -32768, 32767).astype(np.int16).tobytes()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е"))


def is_echo(heard: str, spoken: str) -> bool:
    """Похоже ли распознанное на то, что Джарвис сам только что произнёс."""
    hw, sw = _words(heard), _words(spoken)
    if not hw or not sw:
        return False
    spoken_set = set(sw)
    overlap = sum(w in spoken_set for w in hw) / len(hw)
    seq = difflib.SequenceMatcher(None, " ".join(hw), " ".join(sw)).find_longest_match()
    return overlap >= 0.6 or seq.size >= max(12, len(" ".join(hw)) * 0.6)


class _Vosk:
    """Локальное потоковое распознавание (необязательно): живые подсказки и быстрый результат."""

    def __init__(self):
        self.rec = None
        if not VOSK_MODEL_DIR.exists():
            return
        try:
            import vosk
            vosk.SetLogLevel(-1)
            self.model = vosk.Model(str(VOSK_MODEL_DIR))
            self.vosk = vosk
            self.reset()
            print("[mic] Vosk: живая расшифровка включена")
        except Exception as e:
            print(f"[mic] Vosk недоступен: {e}")
            self.rec = None

    @property
    def ok(self) -> bool:
        return self.rec is not None

    def reset(self) -> None:
        self.rec = self.vosk.KaldiRecognizer(self.model, SAMPLE_RATE)

    def feed(self, data: bytes) -> str:
        """Возвращает текущую частичную расшифровку."""
        import json
        if self.rec.AcceptWaveform(data):
            self._done = json.loads(self.rec.Result()).get("text", "")
            self._acc = (getattr(self, "_acc", "") + " " + self._done).strip()
            return self._acc
        part = json.loads(self.rec.PartialResult()).get("partial", "")
        return (getattr(self, "_acc", "") + " " + part).strip()

    def final(self) -> str:
        import json
        last = json.loads(self.rec.FinalResult()).get("text", "")
        text = (getattr(self, "_acc", "") + " " + last).strip()
        self._acc = ""
        self.reset()
        return text


class Listener(threading.Thread):
    def __init__(self, hub):
        super().__init__(daemon=True, name="listener")
        self.hub = hub
        self.echo_k = 0.3          # мик_уровень ≈ echo_k × уровень_голоса_Джарвиса (подстраивается)
        self.stt_engine = os.getenv("JARVIS_STT", "google").lower()   # google | vosk

    def run(self) -> None:
        import numpy as np
        import sounddevice as sd
        import speech_recognition as sr

        recognizer = sr.Recognizer()
        vosk = _Vosk()
        chunk = int(SAMPLE_RATE * CHUNK_SEC)
        mics = pick_microphones(sd)
        attempt = 0
        speaker = self.hub.speaker
        ref_hist = collections.deque(maxlen=8)   # уровень голоса Джарвиса за последние ~0.4 с

        while True:
            device = mics[attempt % len(mics)]
            name = sd.query_devices(device)["name"]
            try:
                with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=chunk,
                                    device=device) as stream:
                    calib = [float(np.abs(stream.read(chunk)[0]).max()) for _ in range(20)]
                    if max(calib) <= 3 and len(mics) > 1:  # почти абсолютная тишина — микрофон «мёртвый»
                        print(f"[mic] {name}: тишина, пробую следующий")
                        attempt += 1
                        continue
                    print(f"[mic] слушаю: {name}")
                    self._failures = 0
                    self.hub.emit({"type": "device", "mic": True, "name": name})
                    noise = float(np.mean([np.abs(stream.read(chunk)[0]).mean() for _ in range(8)]))
                    preroll = collections.deque(maxlen=6)
                    frames, started, silent, t0, last_ui, last_level = [], False, 0.0, 0.0, 0.0, -1.0
                    over_since, barged, during_reply, last_partial = None, False, False, ""

                    while True:
                        data, _ = stream.read(chunk)
                        level = float(np.abs(data).mean())
                        noise_thr = max(MIN_THRESHOLD, noise * 3.0)
                        ref_hist.append(speaker.ref_level if speaker.playing else 0.0)

                        if not self.hub.mic_on:
                            frames, started = [], False
                            preroll.clear()
                            continue

                        state = self.hub.state
                        replying = state in ("thinking", "speaking")
                        jarvis_voice = max(ref_hist)

                        # ── порог: при голосе Джарвиса ждём эхо и реагируем только на то, что громче ──
                        if speaker.playing:
                            if speaker.ref_known:
                                expected_echo = self.echo_k * jarvis_voice
                                threshold = max(noise_thr * 1.5, expected_echo * ECHO_MARGIN)
                            else:  # mp3-голос Edge: громкость неизвестна — порог просто выше
                                threshold = max(noise_thr * 4, 900)
                        else:
                            threshold = noise_thr

                        now = time.time()
                        # уровень для сферы в HUD: не чаще 10 раз в секунду и только при заметном
                        # изменении — каждое сообщение будит интерфейс (и обои) на перерисовку
                        ui_level = min(1.0, level / (noise_thr * 4))
                        if now - last_ui > 0.1 and abs(ui_level - last_level) > 0.03:
                            self.hub.emit({"type": "level", "value": round(ui_level, 3)})
                            last_ui, last_level = now, ui_level

                        if not started:
                            preroll.append(data.copy())
                            # пока говорит только Джарвис — учимся, насколько громко его слышно в микрофоне
                            if speaker.playing and speaker.ref_known and jarvis_voice > 300:
                                ratio = min(5.0, level / jarvis_voice)
                                self.echo_k = self.echo_k * 0.95 + ratio * 0.05
                            if level > threshold:
                                started, t0, silent = True, now, 0.0
                                frames = list(preroll)
                                during_reply, barged, over_since = replying, False, now
                                if vosk.ok:
                                    for f in frames:
                                        vosk.feed(f.tobytes())
                                self.hub.emit({"type": "vad", "speaking": True})
                            elif not speaker.playing:  # подстраиваемся под фоновый шум комнаты
                                noise = noise * 0.97 + level * 0.03
                            continue

                        frames.append(data.copy())
                        if vosk.ok:
                            partial = vosk.feed(data.tobytes())
                            if partial and partial != last_partial:
                                last_partial = partial
                                self.hub.emit({"type": "partial", "text": partial})

                        # ── перебивание: устойчиво громче эха → ставим ответ на паузу ──
                        # Обрываем ответ только когда убедимся, что это речь, а не щелчок или шум.
                        if during_reply and not barged and self.hub.barge_enabled:
                            if level > threshold:
                                if now - over_since >= BARGE_SUSTAIN_SEC:
                                    barged = True
                                    self.hub.barge_pause()
                            else:
                                over_since = now

                        silent = silent + CHUNK_SEC if level < threshold else 0.0
                        if silent < END_SILENCE_SEC and now - t0 < MAX_PHRASE_SEC:
                            continue

                        # ── фраза закончилась ──
                        self.hub.emit({"type": "vad", "speaking": False})
                        duration = now - t0 - silent
                        audio = np.concatenate(frames).tobytes()
                        frames, started, last_partial = [], False, ""
                        preroll.clear()
                        local_text = vosk.final() if vosk.ok else ""
                        if duration < MIN_PHRASE_SEC:
                            if barged:
                                self.hub.barge_resume()   # короткий звук — продолжаем говорить
                            continue

                        if self.stt_engine == "vosk" and local_text:
                            text = local_text
                        else:
                            text = self._recognize(recognizer, sr, _normalize(np, audio)) or local_text
                        label = " (во время ответа)" if during_reply else ""
                        # ответ «да/нет» на запрос подтверждения эхом не считаем (в вопросе тоже есть «да»)
                        echo_check = during_reply and not self.hub.confirm
                        if echo_check and (not text or is_echo(text, speaker.recent_text())):
                            print(f"[mic] фраза {duration:.1f} с{label} → шум/эхо «{text}», продолжаю ответ")
                            if barged:
                                self.hub.barge_resume()
                            continue
                        print(f"[mic] фраза {duration:.1f} с{label} → «{text or '(не разобрал)'}»")
                        if text:
                            self.hub.submit_voice(text, interrupt=during_reply)
                        elif duration > 1.0:
                            self.hub.emit({"type": "heard", "text": "не разобрал, повторите"})
            except Exception as e:
                self.hub.emit({"type": "device", "mic": False})
                print(f"[mic] «{name}» недоступен: {e}")
                if speaker.paused:  # микрофон отвалился посреди «прислушивания» — продолжаем ответ
                    self.hub.barge_resume()
                failures = getattr(self, "_failures", 0) + 1
                self._failures = failures
                if failures >= 3:
                    self.hub.toast(f"Микрофон «{name}» недоступен: {e}")
                time.sleep(2)
                # устройство могло на миг пропасть (подключили наушники, перезапустилось аудио):
                # заново опрашиваем систему и начинаем с лучшего микрофона
                try:
                    sd._terminate()
                    sd._initialize()
                except Exception:
                    pass
                mics = pick_microphones(sd)
                attempt = 0 if failures < 3 else attempt + 1
                continue

    def _recognize(self, recognizer, sr, audio: bytes) -> str:
        try:
            return recognizer.recognize_google(sr.AudioData(audio, SAMPLE_RATE, 2), language="ru-RU")
        except sr.UnknownValueError:
            return ""
        except sr.RequestError:
            self.hub.toast("Сервис распознавания речи недоступен — проверьте интернет.")
            return ""
