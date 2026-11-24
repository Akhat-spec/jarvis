"""
J.A.R.V.I.S. — запуск: python run.py
Поднимает локальный сервер HUD, голосовой цикл и открывает окно интерфейса.
"""
import asyncio
import functools
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from websockets.asyncio.server import serve

from core import actions, winctl
from core import settings as S
from core import tools as T
from core.sysinfo import SysMonitor
from core.weather import Weather
from core.brain import Brain
from core.safety import needs_confirmation
from core.config import (EXIT_WORDS, FOLLOW_UP_SECONDS, HTTP_PORT, HUD_DIR, ROOT, STOP_HOTKEY,
                         USER_TITLE, WAKE_WORDS, WS_PORT, FAST_MODEL, AGENT_MODEL)
from core.listener import Listener
from core.speaker import Speaker


class Hub:
    """Центр: хранит состояние, рассылает события в HUD, связывает слух, мозг и голос."""

    def __init__(self, background: bool = True):
        """background=False — без фоновых работ (показатели, погода, Google, картинки плиток): для тестов,
        чтобы проверка ядра не ходила в интернет и в почту."""
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients = set()
        self.state = "idle"            # idle | listening | thinking | speaking
        self.mic_on = True
        self.wake_required = True
        self.barge_enabled = True      # перебивание голосом
        self.awake_until = 0.0
        self.camera_on = False
        self.autonomy = "auto"         # chat | confirm | auto (используется с этапа 3)
        self.compact = False
        self.pinned = False
        self.task: asyncio.Task | None = None
        self.brain = Brain()
        self.speaker = Speaker(on_start=self._on_tts_start, on_error=self.toast)
        self.ctx = T.Ctx(self.brain)
        self.ctx.on_capture = lambda kind: self.emit({"type": "capture", "kind": kind})
        self._snap = None
        self.agent_active = False
        self.confirm: dict | None = None   # ожидающее подтверждение {"id", "future", "text"}
        self._confirm_seq = 0

        # настройки HUD (переживают перезапуск) и новые функции интерфейса
        self.settings = S.load()
        self.wake_required = bool(self.settings["wake"])
        self.barge_enabled = bool(self.settings["barge"])
        self.ctx.screen_allowed = bool(self.settings["screen"])
        if self.settings["autonomy"] in ("chat", "confirm", "auto"):
            self.autonomy = self.settings["autonomy"]
        self.speaker.muted = not self.settings["voice"]
        self.sleeping = False
        self.ctx.get_city = lambda: self.settings.get("city", "")
        self.google = {"state": "unknown"}
        self.google_summary: dict | None = None
        self._google_wake = threading.Event()
        self.sysmon = self.weather = None
        if background:
            self.sysmon = SysMonitor(self.emit, lambda: bool(self.clients))
            self.weather = Weather(self.emit, lambda: self.settings.get("city", ""))
            self.refresh_tile_images()
            threading.Thread(target=self._google_loop, daemon=True, name="google").start()

    async def watch_awake(self) -> None:
        """Окно продолжения разговора истекло — сообщаем HUD, чтобы «СЛУШАЮ…» сменилось на «ОЖИДАНИЕ»."""
        was = None
        while True:
            awake = time.time() < self.awake_until
            if awake != was:
                if was is not None:
                    await self.broadcast(self.status())
                was = awake
            await asyncio.sleep(0.5)

    # ── Google: состояние подключения и сводка (непрочитанные письма, ближайшее событие) ──
    def _google_loop(self) -> None:
        from core import google_services as G
        while True:
            try:
                self.google = G.status()
            except Exception as e:
                self.google = {"state": "error", "error": str(e)[:200]}
            summary = None
            if self.google.get("state") == "connected":
                try:
                    summary = {"type": "google", "unread": G.gmail_unread_count(), "next": G.next_event()}
                except Exception as e:
                    print(f"[google] {type(e).__name__}: {e}")
            self.google_summary = summary or {"type": "google", "unread": None, "next": None}
            self.emit(self.settings_msg())
            self.emit(self.google_summary)
            self._google_wake.wait(300)  # раз в 5 минут
            self._google_wake.clear()

    async def google_connect(self) -> None:
        from core import google_services as G
        if getattr(self, "_google_connecting", False):  # повторное нажатие — не запускаем второе ожидание
            self.toast("Подключение к Google уже идёт — завершите его в браузере.")
            return
        self._google_connecting = True
        self.toast("Открываю страницу Google — разрешите доступ в браузере.")
        try:
            await asyncio.to_thread(G.connect)
            self.toast("Google подключён.")
        except Exception as e:
            self.toast(f"Google не подключён: {e}")
        finally:
            self._google_connecting = False
        self._google_wake.set()

    def refresh_tile_images(self) -> None:
        """Обложки игр и значки программ — в фоне; когда готовы, HUD получает обновлённые плитки."""
        def job():
            from core import tileicons
            if tileicons.add_images(self.settings["tiles"]):
                S.save(self.settings)
                self.emit(self.settings_msg())
        threading.Thread(target=job, daemon=True, name="tile-images").start()

    def save_settings(self) -> None:
        self.settings.update(wake=self.wake_required, barge=self.barge_enabled, screen=self.ctx.screen_allowed,
                             autonomy=self.autonomy, voice=not self.speaker.muted)
        S.save(self.settings)

    def settings_msg(self) -> dict:
        s = self.settings
        return {"type": "settings", "log": s["log"], "hints": s["hints"], "city": s["city"],
                "memory": s["memory"], "tiles": s["tiles"], "google": getattr(self, "google", {"state": "unknown"})}

    def tool_defs(self, for_agent: bool = False) -> list[dict]:
        defs = T.definitions(self.autonomy, for_agent=for_agent)
        if not self.settings["memory"]:  # «Память» выключена — новые факты не запоминаем
            defs = [d for d in defs if d["name"] != "remember_fact"]
        return defs

    # ── рассылка в HUD ──
    async def broadcast(self, msg: dict) -> None:
        raw = json.dumps(msg, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send(raw)
            except Exception:
                self.clients.discard(ws)

    def emit(self, msg: dict) -> None:
        """Потокобезопасная отправка события (можно вызывать из любых потоков)."""
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.broadcast(msg), self.loop)

    def toast(self, text: str) -> None:
        print(f"[i] {text}")
        self.emit({"type": "toast", "text": text})

    def status(self) -> dict:
        return {
            "type": "status", "state": self.state, "mic": self.mic_on, "camera": self.camera_on,
            "autonomy": self.autonomy, "wake": self.wake_required, "barge": self.barge_enabled,
            "compact": self.compact, "tts": self.speaker.provider, "synth_ms": self.speaker.last_synth_ms,
            "pinned": self.pinned, "awake": time.time() < self.awake_until,
            "model": AGENT_MODEL if self.agent_active else FAST_MODEL, "agent": self.agent_active,
            "screen": self.ctx.screen_allowed,
            "sleeping": self.sleeping, "voice": not self.speaker.muted,
        }

    async def set_state(self, state: str) -> None:
        self.state = state
        await self.broadcast(self.status())

    def _on_tts_start(self, text: str) -> None:
        self.emit({"type": "speaking_text", "text": text})  # субтитры в HUD
        if self.state != "speaking":
            self.state = "speaking"
            self.emit(self.status())

    # ── слух ──
    def submit_voice(self, text: str, interrupt: bool = False) -> None:
        """Фраза распознана. interrupt=True — сказана поверх ответа (слово «Джарвис» не нужно)."""
        if interrupt:
            self.awake_until = time.time() + FOLLOW_UP_SECONDS
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.on_voice(text), self.loop)

    def barge_pause(self) -> None:
        """Похоже, пользователь заговорил поверх ответа: замолкаем и слушаем (из потока слуха).
        Если это была речь — submit_voice оборвёт ответ; если шум — barge_resume продолжит его."""
        self.speaker.pause()
        self.emit({"type": "hold", "on": True})

    def barge_resume(self) -> None:
        self.speaker.resume()
        self.emit({"type": "hold", "on": False})

    async def on_voice(self, heard: str) -> None:
        low = heard.lower()
        await self.broadcast({"type": "heard", "text": heard})
        if self.confirm and (answer := _yes_no(heard)) is not None:
            self.resolve_confirm(answer)
            return
        if self.sleeping:  # спящий режим: реагируем только на «Джарвис»
            if any(w in low for w in WAKE_WORDS):
                await self.set_sleep(False)
                self.awake_until = time.time() + FOLLOW_UP_SECONDS
                await self.say_line(f"Слушаю, {USER_TITLE}.")
            return
        if self.speaker.paused:
            # речь прозвучала поверх ответа и это не «да/нет» — старый ответ больше не нужен
            # (иначе озвучка так и осталась бы на паузе, а Джарвис замолчал бы навсегда)
            await self.cancel()
            await self.broadcast({"type": "hold", "on": False})
        # «отключись» — только если обращаются к Джарвису (слово «Джарвис» или окно продолжения
        # разговора): иначе фраза из видео или чужого разговора выключала бы микрофон
        addressed = any(w in low for w in WAKE_WORDS) or not self.wake_required or time.time() <= self.awake_until
        if addressed and any(w in low for w in EXIT_WORDS):
            await self.say_line(f"Перехожу в режим ожидания, {USER_TITLE}. Микрофон выключен.")
            self.mic_on = False
            await self.broadcast(self.status())
            return

        command = heard
        if self.wake_required and time.time() > self.awake_until:
            for w in WAKE_WORDS:
                if w in low:
                    command = heard[low.index(w) + len(w):].strip(" ,.!?")
                    break
            else:  # фраза не к Джарвису — подсказываем в HUD
                await self.broadcast({"type": "ignored", "text": heard})
                return
            if not command:
                self.awake_until = time.time() + FOLLOW_UP_SECONDS
                await self.say_line(f"Слушаю, {USER_TITLE}.")
                return
        await self.handle_user(command)

    # ── диалог ──
    async def set_sleep(self, on: bool) -> None:
        self.sleeping = on
        if on:
            await self.cancel()
            self.awake_until = 0.0
        await self.broadcast(self.status())

    async def handle_user(self, text: str) -> None:
        if self.sleeping:  # команда текстом будит Джарвиса
            await self.set_sleep(False)
        if self.state in ("thinking", "speaking"):
            await self.broadcast({"type": "interrupted"})
        await self.broadcast({"type": "hold", "on": False})
        await self.cancel()
        actions.ABORT.clear()
        await self.broadcast({"type": "msg", "role": "user", "text": text})
        self.task = asyncio.create_task(self._respond(text))

    # ── действия (инструменты) ──
    async def run_tool(self, name: str, args) -> tuple[str, bool]:
        """Проверка → оценка риска → (подтверждение) → выполнение. Возвращает (результат, ошибка?)."""
        err = T.validate(name, args)
        if err:
            return f"Ошибка аргументов: {err}", True
        try:
            risk, desc = await asyncio.to_thread(T.assess, name, args, self.ctx)
        except Exception as e:
            return f"Не удалось оценить действие: {e}", True

        decision = needs_confirmation(risk, self.autonomy)
        if decision is None:
            why = "запрещено правилами безопасности" if risk == "blocked" else \
                "в режиме «Разговор» действия на компьютере отключены"
            await self._action_log(desc, risk, "запрещено")
            return f"Отказано: {why}. Действие: {desc}", True
        if decision:
            ok = await self.ask_confirm(desc, risk)
            if not ok:
                await self._action_log(desc, risk, "отклонено")
                return ("Пользователь ответил «нет» (или не ответил) — действие отменено. Коротко скажи, "
                        "что отменил, и не повторяй его без новой просьбы."), True

        await self._action_log(desc, risk, "выполняю")
        try:
            if name == "look_through_camera":
                result = await self.camera_snapshot(args.get("reason", ""))
            elif name == "delegate_task":
                result = await self.run_agent(args["task"])
            else:
                result = await asyncio.to_thread(T.TOOLS[name].run, args, self.ctx)
        except actions.AbortError as e:
            await self._action_log(desc, risk, "остановлено")
            return f"Остановлено: {e}", True
        except Exception as e:
            await self._action_log(desc, risk, f"ошибка: {e}")
            return f"Ошибка: {e}", True
        if isinstance(result, dict) and result.get("image"):  # снимок экрана/камеры → картинка для Claude
            result = [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": result["image"]}},
                {"type": "text", "text": result.get("text", "")},
            ]
        await self._action_log(desc, risk, "готово", result)
        return result, False

    # ── агентный контур (Opus) ──
    async def run_agent(self, task: str) -> str:
        self.agent_active = True
        await self.broadcast(self.status())
        await self.broadcast({"type": "agent", "on": True, "task": task})
        last_said = [0.0]

        async def on_status(text: str | None = None, tool: str | None = None):
            now = time.time()
            if text:
                text = " ".join(text.split())
                await self.broadcast({"type": "agent_note", "text": text})
            phrase = text if text and len(text) <= 70 else _STATUS_PHRASES.get(tool)
            # не тараторим: не чаще раза в 6 секунд и только пока ничего не звучит
            if phrase and now - last_said[0] > 6 and not self.speaker.busy:
                last_said[0] = now
                self.speaker.say(phrase)

        try:
            return await self.brain.run_agent(
                task, self.tool_defs(for_agent=True), self.run_tool, on_status)
        finally:
            self.agent_active = False
            await self.broadcast({"type": "agent", "on": False})
            await self.broadcast(self.status())

    # ── камера (живёт в HUD: доступ к ней даёт только кнопка пользователя) ──
    async def camera_snapshot(self, reason: str) -> dict:
        if not self.camera_on:
            await self.broadcast({"type": "camera_request", "reason": reason})
            self.speaker.say(f"Включите камеру кнопкой в панели, {USER_TITLE}.")
            for _ in range(200):  # ждём до 20 с, пока пользователь сам нажмёт «Камера»
                if self.camera_on:
                    break
                await asyncio.sleep(0.1)
            else:
                raise PermissionError("Пользователь не включил камеру. Не проси снова без его просьбы.")
            await asyncio.sleep(0.8)  # камере нужно время на экспозицию
        if not self.clients:
            raise RuntimeError("Окно HUD закрыто — камеру взять неоткуда.")
        self._snap_seq = getattr(self, "_snap_seq", 0) + 1
        fut = self.loop.create_future()
        self._snap = (self._snap_seq, fut)
        await self.broadcast({"type": "camera_snap", "id": self._snap_seq, "reason": reason})
        try:
            data = await asyncio.wait_for(fut, 8)
        except asyncio.TimeoutError:
            raise RuntimeError("HUD не прислал кадр с камеры.") from None
        finally:
            self._snap = None
        if not data:
            raise RuntimeError("Камера не отдала кадр.")
        self.ctx.last_look = time.time()
        return {"image": data, "text": "Кадр с веб-камеры пользователя."}

    async def _action_log(self, desc: str, risk: str, status: str, result=None) -> None:
        print(f"[act] {risk:9s} {status:10s} {desc}")
        if status == "выполняю":
            return
        # В память пишем действие И его результат — иначе в следующих ответах модель видит свои
        # цифры без доказательств и начинает «извиняться», что ничего не проверяла.
        short = re.sub(r"\s*\((команда[^)]*)\)\s*$", "", desc)[:160]
        entry = f"{short} — {status}"
        if isinstance(result, list):
            entry += " (снимок получен и просмотрен)"
        elif isinstance(result, str) and result.strip():
            entry += f"; результат: {' '.join(result.split())[:300]}"
        self.brain.turn_actions.append(entry)
        await self.broadcast({"type": "action", "text": desc, "risk": risk, "status": status})

    async def ask_confirm(self, desc: str, risk: str, timeout: float = 40) -> bool:
        self._confirm_seq += 1
        fut = self.loop.create_future()
        self.confirm = {"id": self._confirm_seq, "future": fut, "text": desc}
        await self.broadcast({"type": "confirm", "id": self._confirm_seq, "text": desc, "risk": risk,
                              "timeout": timeout})
        self.speaker.say(f"Подтвердите, {USER_TITLE}: {desc}. Да или нет?")
        self.awake_until = time.time() + timeout
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.speaker.say("Не дождался ответа, отменяю.")
            return False
        finally:
            self.confirm = None
            await self.broadcast({"type": "confirm_done"})
            await self.set_state("thinking")

    def resolve_confirm(self, ok: bool) -> None:
        if self.confirm and not self.confirm["future"].done():
            self.confirm["future"].set_result(ok)
        self.speaker.stop()  # недоговорённый вопрос больше не нужен
        self.emit({"type": "hold", "on": False})

    async def _respond(self, text: str) -> None:
        await self.set_state("thinking")
        await self.broadcast({"type": "msg_start", "role": "assistant"})

        async def on_delta(chunk: str):
            await self.broadcast({"type": "msg_delta", "text": chunk})

        self.brain.autonomy = self.autonomy
        try:
            await self.brain.reply(text, on_delta, self.speaker.say,
                                   tools=self.tool_defs(), run_tool=self.run_tool)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            line = _api_error_line(e)
            await self.broadcast({"type": "msg_delta", "text": line})
            self.speaker.say(line)
        await self.broadcast({"type": "msg_end"})
        await self._wait_speech()

    async def say_line(self, text: str) -> None:
        await self.broadcast({"type": "msg", "role": "assistant", "text": text})
        self.speaker.say(text)
        await self._wait_speech()

    async def _wait_speech(self) -> None:
        while self.speaker.busy:
            await asyncio.sleep(0.05)
        self.awake_until = time.time() + FOLLOW_UP_SECONDS
        await self.set_state("listening" if self.mic_on else "idle")

    async def cancel(self) -> None:
        if self.confirm and not self.confirm["future"].done():
            self.confirm["future"].set_result(False)
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self.speaker.stop()

    async def emergency_stop(self) -> None:
        actions.ABORT.set()  # мышь и клавиатура останавливаются на следующем шаге
        await self.cancel()
        await self.broadcast({"type": "msg_end"})
        await self.broadcast({"type": "alert", "text": "АВАРИЙНАЯ ОСТАНОВКА"})
        await self.set_state("listening" if self.mic_on else "idle")

    # ── команды из HUD ──
    async def on_hud(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "confirm_answer":
            if self.confirm and self.confirm["id"] == msg.get("id"):
                self.resolve_confirm(bool(msg.get("ok")))
        elif t == "text" and isinstance(msg.get("text"), str) and msg["text"].strip():
            text = msg["text"].strip()[:4000]
            if self.confirm and (answer := _yes_no(text)) is not None:
                self.resolve_confirm(answer)
            else:
                await self.handle_user(text)
        elif t == "stop":
            await self.emergency_stop()
        elif t == "mic":
            self.mic_on = bool(msg.get("on"))
            if not self.mic_on and self.state == "listening":
                self.state = "idle"
            elif self.mic_on and self.state == "idle":
                self.state = "listening"
        elif t == "wake":
            self.wake_required = bool(msg.get("on"))
            self.save_settings()
        elif t == "barge":
            self.barge_enabled = bool(msg.get("on"))
            self.save_settings()
        elif t == "sleep":
            await self.set_sleep(bool(msg.get("on")))
            if not self.sleeping:
                self.awake_until = time.time() + FOLLOW_UP_SECONDS
        elif t == "ptt":  # пробел удерживается: слушаем без слова «Джарвис»
            if not self.sleeping:
                self.awake_until = time.time() + FOLLOW_UP_SECONDS
        elif t == "voice":
            self.speaker.muted = not bool(msg.get("on"))
            if self.speaker.muted:
                self.speaker.stop()
            self.save_settings()
        elif t == "pref" and msg.get("key") in ("log", "hints", "memory"):
            self.settings[msg["key"]] = bool(msg.get("on"))
            S.save(self.settings)
            await self.broadcast(self.settings_msg())
        elif t == "city" and isinstance(msg.get("city"), str):
            self.settings["city"] = msg["city"].strip()[:80]
            S.save(self.settings)
            if self.weather:
                self.weather.refresh()
            await self.broadcast(self.settings_msg())
        elif t == "google_connect":
            asyncio.create_task(self.google_connect())
            return
        elif t == "google_disconnect":
            from core import google_services as G
            await asyncio.to_thread(G.disconnect)
            self._google_wake.set()
            self.toast("Google отключён: ключ доступа удалён с компьютера.")
            return
        elif t in ("launch", "tile_add", "tile_remove", "tile_pick"):
            await self.on_tile(t, msg)
            return
        elif t == "camera":  # камера живёт в HUD; бэкенд только знает, что она включена
            self.camera_on = bool(msg.get("on"))
        elif t == "camera_frame":
            # отвечает то окно HUD, где камера реально включена; пустые ответы других окон пропускаем
            if self._snap and self._snap[0] == msg.get("id") and msg.get("data") and not self._snap[1].done():
                self._snap[1].set_result(msg["data"])
            return
        elif t == "screen":
            self.ctx.screen_allowed = bool(msg.get("on"))
            self.save_settings()
        elif t == "autonomy" and msg.get("level") in ("chat", "confirm", "auto"):
            self.autonomy = msg["level"]
            self.save_settings()
        elif t == "compact":
            self.compact = bool(msg.get("on"))
            await asyncio.to_thread(winctl.set_compact, self.compact)
        elif t == "pin":
            self.pinned = bool(msg.get("on"))
            await asyncio.to_thread(winctl.set_topmost, self.pinned)
        await self.broadcast(self.status())


    # ── плитки «Программы / Игры / Файлы» ──
    async def on_tile(self, t: str, msg: dict) -> None:
        tab = msg.get("tab")
        tiles = self.settings["tiles"].get(tab) if tab in S.TABS else None
        if tiles is None:
            return
        if t == "launch":
            idx = msg.get("idx")
            if not isinstance(idx, int) or not 0 <= idx < len(tiles):
                return
            item = tiles[idx]
            try:
                await asyncio.to_thread(S.launch, item["target"])
                await self._action_log(f"открыть «{item['name']}» (плитка)", "normal", "готово")
            except Exception as e:
                self.toast(f"Не удалось открыть «{item['name']}»: {e}")
            return
        if t == "tile_pick":  # выбор через Проводник — без ручного ввода пути
            if getattr(self, "_picking", False):
                return
            self._picking = True
            try:
                path = await asyncio.to_thread(S.pick_path, tab, "folder" if msg.get("kind") == "folder" else "file")
            finally:
                self._picking = False
            if not path:
                return
            err = S.check_target(path)
            if err:
                self.toast(err)
                return
            item = S.tile_from_path(tab, path)
            if any(x["target"].lower() == item["target"].lower() for x in tiles):
                self.toast(f"«{item['name']}» уже есть на вкладке")
                return
            tiles.append(item)
            self.toast(f"Добавлено: «{item['name']}»")
            self.refresh_tile_images()
        elif t == "tile_add":
            name = str(msg.get("name", "")).strip()[:40]
            target = str(msg.get("target", "")).strip()[:400]
            err = None if name else "Укажите название"
            err = err or S.check_target(target)
            if err:
                self.toast(err)
                return
            tiles.append({"name": name, "hint": msg.get("hint") or "добавлено вами", "target": target.strip('"')})
            self.refresh_tile_images()
        elif t == "tile_remove":
            idx = msg.get("idx")
            if isinstance(idx, int) and 0 <= idx < len(tiles):
                tiles.pop(idx)
        S.save(self.settings)
        await self.broadcast(self.settings_msg())


_STATUS_PHRASES = {
    "look_at_screen": "Смотрю на экран.", "click": "Нажимаю.", "type_text": "Печатаю.",
    "run_command": "Выполняю команду.", "write_text_file": "Записываю файл.", "open_app": "Открываю программу.",
    "open_website": "Открываю сайт.", "focus_window": "Переключаю окно.", "read_text_file": "Читаю файл.",
    "list_directory": "Смотрю папку.", "look_through_camera": "Смотрю через камеру.",
}

_NO = ("нет", "не надо", "не нужно", "отмен", "стоп", "запрещаю", "no")
_YES = ("да", "подтверждаю", "выполняй", "давай", "делай", "разрешаю", "можно", "ок", "окей", "ага",
        "угу", "конечно", "yes")


def _yes_no(text: str) -> bool | None:
    import re
    words = re.findall(r"[а-яёa-z]+", text.lower())
    joined = " ".join(words)
    if any(joined.startswith(n) or f" {n}" in f" {joined}" for n in _NO):
        return False
    if words and (words[0] in _YES or (len(words) > 1 and words[1] in _YES and words[0] in ("джарвис", "jarvis"))):
        return True
    return None


def _api_error_line(e: Exception) -> str:
    import anthropic
    if isinstance(e, anthropic.AuthenticationError) or "authentication method" in str(e):
        return "Не могу подключиться к мозгу: проверьте ключ ANTHROPIC_API_KEY в файле .env."
    if isinstance(e, anthropic.RateLimitError):
        return "Слишком много запросов. Дайте мне секунду."
    if isinstance(e, anthropic.APIConnectionError):
        return "Нет связи с сервером. Проверьте интернет."
    if isinstance(e, anthropic.APIStatusError):
        return f"Ошибка сервера {e.status_code}."
    return f"Внутренняя ошибка: {e}"


# ── HTTP-сервер для файлов HUD ──
class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def start_http() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), functools.partial(_Quiet, directory=str(HUD_DIR)))
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()


def open_hud_window() -> None:
    url = f"http://127.0.0.1:{HTTP_PORT}/"
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    edge = next((p for p in candidates if p.exists()), None)
    if edge:
        profile = ROOT / ".hud_profile"  # отдельный профиль: окно-приложение и свои разрешения камеры
        subprocess.Popen([str(edge), f"--app={url}", f"--user-data-dir={profile}",
                          "--window-size=1280,800", "--no-first-run", "--disable-features=Translate"])
    else:
        webbrowser.open(url)


# Окно интерфейса открывается только по --window: основной интерфейс — живые обои рабочего стола.
WANT_WINDOW = "--window" in sys.argv


def launch_wallpaper() -> None:
    """Живые обои (wallpaper.py сам не даст запустить вторую копию и уважает «выключено»)."""
    if "--no-wallpaper" in sys.argv:
        return
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    try:
        subprocess.Popen([str(pyw if pyw.exists() else exe), str(ROOT / "wallpaper.py"), "--auto"], cwd=str(ROOT))
    except OSError as e:
        print(f"[!] обои не запустились: {e}")


# ── обои Wallpaper Engine ──
def _ws_token() -> str:
    """Секретный ключ для обоев Wallpaper Engine (страница-файл, Origin «null»). Хранится в .ws_token."""
    f = ROOT / ".ws_token"
    try:
        t = f.read_text(encoding="utf-8").strip()
        if len(t) >= 32:
            return t
    except OSError:
        pass
    import secrets
    t = secrets.token_hex(24)
    f.write_text(t, encoding="utf-8")
    return t


def _find_we_project() -> Path:
    """Папка проекта обоев в Wallpaper Engine: ищем WE во всех библиотеках Steam.
    Если JARVIS_WE=1 (выбрано при установке), а проекта ещё нет — создаём его."""
    libs = [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam"]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            libs.insert(0, Path(winreg.QueryValueEx(k, "SteamPath")[0]))
        vdf = (libs[0] / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="ignore")
        libs += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf)]
    except OSError:
        pass
    for lib in libs:
        we = lib / "steamapps" / "common" / "wallpaper_engine"
        if (we / "wallpaper64.exe").exists() or (we / "wallpaper32.exe").exists():
            proj = we / "projects" / "myprojects" / "jarvis"
            if not proj.exists() and os.getenv("JARVIS_WE") == "1":
                try:
                    proj.mkdir(parents=True)
                except OSError as e:
                    print(f"[!] не удалось создать проект обоев Wallpaper Engine: {e}")
            return proj
    return libs[-1] / "steamapps" / "common" / "wallpaper_engine" / "projects" / "myprojects" / "jarvis"


WS_TOKEN = _ws_token()
WE_PROJECT = _find_we_project()


def sync_we_project() -> None:
    """Обновляет проект обоев в Wallpaper Engine из wallpaper/ и hud/ (если он установлен) —
    чтобы правки интерфейса доходили и до обоев. Ключ пишется в we_config.js."""
    if not WE_PROJECT.exists():
        return
    import shutil
    try:
        for name in ("index.html", "project.json", "preview.jpg"):
            src = ROOT / "wallpaper" / name
            if src.exists():
                shutil.copy2(src, WE_PROJECT / name)
        (WE_PROJECT / "hud").mkdir(exist_ok=True)
        for name in ("index.html", "app.js", "style.css"):
            shutil.copy2(HUD_DIR / name, WE_PROJECT / "hud" / name)
        (WE_PROJECT / "we_config.js").write_text(
            f'window.JARVIS_WE = {{ token: "{WS_TOKEN}" }};\n', encoding="utf-8")
    except OSError as e:
        print(f"[!] проект обоев Wallpaper Engine не обновлён: {e}")


def already_running() -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", WS_PORT)) == 0


def setup_logging() -> None:
    """Без консоли (pythonw, автозапуск) пишем журнал в logs/jarvis.log."""
    if sys.stdout is None or "--log" in sys.argv:
        log_dir = ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        log = log_dir / "jarvis.log"
        if log.exists() and log.stat().st_size > 2 * 2**20:  # не даём журналу расти бесконечно
            log.replace(log_dir / "jarvis.old.log")
        f = open(log, "a", encoding="utf-8", buffering=1)
        f.write(f"\n===== запуск {time.strftime('%d.%m.%Y %H:%M:%S')} =====\n")
        sys.stdout = sys.stderr = f


def greeting() -> str:
    h = time.localtime().tm_hour
    part = ("Доброе утро" if 5 <= h < 12 else "Добрый день" if h < 18 else
            "Добрый вечер" if h < 23 else "Доброй ночи")
    return f"{part}, {USER_TITLE}. Все системы в норме."


async def main() -> None:
    if already_running():  # второй запуск: окно — только по --window, обои — если не запущены
        print("J.A.R.V.I.S. уже запущен.")
        if WANT_WINDOW:
            open_hud_window()
        launch_wallpaper()
        return
    hub = Hub()
    hub.loop = asyncio.get_running_loop()

    async def ws_handler(ws):
        # Страница-файл (обои Wallpaper Engine) приходит с Origin «null» — такую пускаем только
        # с секретным ключом из .ws_token (он записан лишь в проект обоев на этом компьютере).
        if ws.request.headers.get("Origin") == "null":
            from urllib.parse import parse_qs, urlparse
            got = (parse_qs(urlparse(ws.request.path).query).get("token") or [""])[0]
            if not hmac.compare_digest(got, WS_TOKEN):
                await ws.close(1008, "token")
                return
        hub.clients.add(ws)
        try:
            await ws.send(json.dumps(hub.status(), ensure_ascii=False))
            await ws.send(json.dumps(hub.settings_msg(), ensure_ascii=False))
            for last in (hub.sysmon and hub.sysmon.last, hub.weather and hub.weather.last, hub.google_summary):
                if last:
                    await ws.send(json.dumps(last, ensure_ascii=False))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    if isinstance(msg, dict):
                        await hub.on_hud(msg)
                except json.JSONDecodeError:
                    pass
                except Exception as e:  # одна кривая команда не должна рвать связь с окном
                    print(f"[!] ошибка обработки команды HUD: {type(e).__name__}: {e}")
        except Exception:
            pass
        finally:
            hub.clients.discard(ws)
            if not hub.clients and hub.camera_on:  # окно закрыто — камеры больше нет
                hub.camera_on = False

    # Только окно самого Джарвиса может управлять ядром. Без этой проверки любой сайт,
    # открытый в браузере, мог бы подключиться к ws://127.0.0.1 и отдавать команды компьютеру.
    # Живые обои (wallpaper.py, свой локальный сервер) только читают состояние, чтобы менять цвет.
    wall_port = int(os.getenv("JARVIS_WALL_PORT", "8768"))
    allowed_origins = [f"http://127.0.0.1:{HTTP_PORT}", f"http://localhost:{HTTP_PORT}",
                       f"http://127.0.0.1:{wall_port}", None, "null"]  # "null" — только с ключом, см. ws_handler

    start_http()
    sync_we_project()
    winctl.start_hotkey(lambda: asyncio.run_coroutine_threadsafe(hub.emergency_stop(), hub.loop),
                        *STOP_HOTKEY)
    async with serve(ws_handler, "127.0.0.1", WS_PORT, origins=allowed_origins, max_size=8 * 2**20):
        print(f"J.A.R.V.I.S. онлайн → http://127.0.0.1:{HTTP_PORT}   (аварийный стоп: Ctrl+Alt+X)")
        if WANT_WINDOW:
            open_hud_window()
        launch_wallpaper()
        Listener(hub).start()
        if WE_PROJECT.exists():  # обои в Wallpaper Engine: он не передаёт колесо — передаём сами
            from core.desktop_wheel import DesktopWheel
            DesktopWheel(hub.emit).start()
        hub._awake_task = asyncio.create_task(hub.watch_awake())  # ссылка — чтобы задачу не собрал GC
        await hub.set_state("listening")
        if "--quiet" not in sys.argv:
            await asyncio.sleep(2.5)  # даём окну открыться
            await hub.say_line(greeting())
        await asyncio.Future()


if __name__ == "__main__":
    setup_logging()
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("[!] ANTHROPIC_API_KEY не найден в .env — интерфейс откроется, но отвечать Джарвис не сможет.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
