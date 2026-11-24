"""Мозг: быстрый контур на Claude с потоковой генерацией.
Текст режется на фразы по мере появления, чтобы озвучка начиналась до конца ответа."""
import asyncio
import json
import re

import anthropic

from .config import AGENT_MODEL, AGENT_PROMPT, FAST_MODEL, HISTORY_LIMIT, MEMORY_FILE, SYSTEM_PROMPT

AGENT_MAX_ROUNDS = 30

_OLD_JOURNAL = re.compile(r"^\[журнал системы: (.*?)\]\n(.*)$", re.S)
_OLD_JOURNAL_ANY = re.compile(r"\[журнал системы:[^\]]*\]\s*")  # если модель всё же «напишет журнал» сама

# Текущий режим автономности (переключатель в HUD) — чтобы модель не гадала, куда делись инструменты
_MODE_NOTE = {
    "chat": "Сейчас включён режим «Разговор»: пользователь отключил действия на компьютере, доступно только "
            "чтение (время, окна, файлы, экран). Если просят действие — скажи, что включён режим «Разговор», "
            "и предложи переключить автономность в панели слева.",
    "confirm": "Режим автономности: «С подтверждением» — любое действие, кроме чтения, пользователь подтверждает.",
    "auto": "Режим автономности: «Авто» — обычные действия сразу, опасные с подтверждением.",
}

_SENTENCE_END = re.compile(r"[.!?…;:](?=\s)|\n")
_MIN_PHRASE = 12  # слишком короткие куски («Да.») склеиваем со следующими
MAX_TOOL_ROUNDS = 12  # предел шагов «действие → результат» за один ответ


_COMMA = re.compile(r"[,—–](?=\s)")


def _cut_point(buf: str, first: bool = False) -> int | None:
    for m in _SENTENCE_END.finditer(buf):
        if m.end() >= _MIN_PHRASE:
            return m.end()
    if first:  # первую фразу режем и по запятой — голос начнёт звучать раньше
        for m in _COMMA.finditer(buf):
            if m.end() >= 25:
                return m.end()
    return None


class Brain:
    def __init__(self):
        self.client = anthropic.AsyncAnthropic()
        self.memory = self._load()
        self._partial = ""
        self.turn_actions: list[str] = []
        self.autonomy = "auto"  # обновляет Hub перед каждым ответом

    # ── память ──
    def _load(self) -> dict:
        if MEMORY_FILE.exists():
            try:
                data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
                data.setdefault("facts", [])
                data.setdefault("history", [])
                for h in data["history"]:  # старый формат: журнал был вписан в текст ответа
                    m = _OLD_JOURNAL.match(h.get("content", "")) if h.get("role") == "assistant" else None
                    if m:
                        h["actions"] = [a.strip() for a in m.group(1).split("; ") if a.strip()]
                        h["content"] = m.group(2).strip() or "…"
                return data
            except json.JSONDecodeError:
                pass
        return {"facts": [], "history": []}

    def save(self) -> None:
        self.memory["history"] = self.memory["history"][-HISTORY_LIMIT:]
        MEMORY_FILE.write_text(json.dumps(self.memory, ensure_ascii=False, indent=2), encoding="utf-8")

    def _api_history(self) -> list[dict]:
        """История для API. Журнал действий идёт отдельным системным сообщением перед ответом —
        если вписать его в текст ответа, модель начинает подражать формату и «пишет журнал» сама
        вместо настоящего вызова инструмента (то есть сообщает о действиях, которых не было)."""
        out = []
        for h in self.memory["history"]:
            if h["role"] == "assistant" and h.get("actions") and out and out[-1]["role"] == "user":
                out.append({"role": "system", "content": "Журнал системы — что реально выполнили инструменты "
                                                         "в ответе ниже: " + "; ".join(h["actions"])})
            out.append({"role": h["role"], "content": h["content"]})
        return out

    def _system(self) -> str:
        from .tools import user_folders
        facts = "\n".join(f"- {f}" for f in self.memory["facts"]) or "пока ничего"
        mode = _MODE_NOTE.get(self.autonomy, "")
        return SYSTEM_PROMPT + f"\n{user_folders()}\n{mode}\n\nЧто ты помнишь о пользователе:\n{facts}"

    # ── ответ ──
    async def reply(self, user_text: str, on_delta, on_sentence, tools=None, run_tool=None) -> str:
        """Стримит ответ. on_delta(кусок) — для ленты HUD, on_sentence(фраза) — для озвучки.
        tools — описания инструментов; run_tool(name, input) → (текст, ошибка?) выполняет их."""
        messages = self._api_history() + [{"role": "user", "content": user_text}]
        try:
            return await self._stream(user_text, messages, on_delta, on_sentence, tools, run_tool)
        except asyncio.CancelledError:
            # Перебили: запоминаем, что успели сказать, чтобы не терять нить разговора
            self._remember(user_text, self._partial.strip() + " …[перебит пользователем]")
            raise

    async def _stream(self, user_text, messages, on_delta, on_sentence, tools, run_tool) -> str:
        full, sent_any = "", False
        self._partial = ""
        self.turn_actions = []  # журнал реальных действий этого ответа (пишет run_tool)
        messages = list(messages)

        for _ in range(MAX_TOOL_ROUNDS):
            buf = ""
            if full and not full[-1].isspace():  # реплики до и после действия — через пробел
                full += " "
                await on_delta(" ")
            params = dict(model=FAST_MODEL, max_tokens=4000, system=self._system(), messages=messages,
                          output_config={"effort": "low"})
            if tools:
                params["tools"] = tools
            async with self.client.messages.stream(**params) as stream:
                async for chunk in stream.text_stream:
                    full += chunk
                    buf += chunk
                    self._partial = full
                    await on_delta(chunk)
                    while (cut := _cut_point(buf, first=not sent_any)) is not None:
                        sentence, buf = buf[:cut], buf[cut:]
                        on_sentence(sentence)
                        sent_any = True
                final = await stream.get_final_message()

            if buf.strip():  # договариваем фразу перед действием («Секунду, открываю…»)
                on_sentence(buf)
                sent_any = True

            if final.stop_reason == "refusal":
                line = "С этим я помочь не могу, сэр."
                full += " " + line
                on_sentence(line)
                break
            if final.stop_reason != "tool_use" or not run_tool:
                break  # end_turn, max_tokens и т.п. — ответ окончен

            # ── выполняем инструменты и отдаём результаты модели ──
            messages.append({"role": "assistant", "content": final.content})
            results = []
            for block in final.content:
                if block.type != "tool_use":
                    continue
                text, is_error = await run_tool(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": text, "is_error": is_error})
            messages.append({"role": "user", "content": results})
        else:
            on_sentence("Слишком много шагов подряд, сэр. Остановлюсь здесь.")

        self._remember(user_text, full)
        return full

    # ── агентный контур: сложные задачи решает Opus ──
    async def run_agent(self, task: str, tools: list, run_tool, on_status) -> str:
        """Многошаговая задача на AGENT_MODEL с теми же инструментами и правилами безопасности.
        on_status(текст) — короткие реплики о ходе работы (озвучиваются). Возвращает итог для пользователя."""
        from .tools import user_folders
        facts = "\n".join(f"- {f}" for f in self.memory["facts"]) or "пока ничего"
        system = AGENT_PROMPT + f"\n{user_folders()}\n\nЧто известно о пользователе:\n{facts}"
        messages = [{"role": "user", "content": task}]
        final_text = ""

        for _ in range(AGENT_MAX_ROUNDS):
            async with self.client.beta.messages.stream(
                model=AGENT_MODEL,
                max_tokens=16000,
                system=system,
                messages=messages,
                tools=tools,
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # если модель откажет по ошибке классификатора — сервер передаст запасной
            ) as stream:
                final = await stream.get_final_message()

            texts = [b.text for b in final.content if b.type == "text" and b.text.strip()]
            if final.stop_reason == "refusal":
                return "Эту задачу я выполнить не могу."
            if final.stop_reason != "tool_use":
                final_text = " ".join(texts).strip()
                break
            for t in texts:  # короткие пояснения между шагами — это голосовой статус
                await on_status(t)

            messages.append({"role": "assistant", "content": final.content})
            results = []
            for block in final.content:
                if block.type != "tool_use":
                    continue
                await on_status(None, block.name)
                content, is_error = await run_tool(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})
        else:
            final_text = "Задача оказалась слишком длинной, остановился на полпути."
        return final_text or "Готово."

    def _compress_actions(self) -> list[str]:
        acts = list(getattr(self, "turn_actions", []))
        if len(acts) > 8:  # длинные задачи агента: первые шаги и итог, без раздувания истории
            acts = acts[:3] + [f"…ещё {len(acts) - 6} шагов…"] + acts[-3:]
        return acts

    def _remember(self, user_text: str, answer: str) -> None:
        """В историю пишем ответ и (отдельно) журнал действий — иначе в следующий раз модель
        не увидит, что действия реально выполнялись."""
        text = _OLD_JOURNAL_ANY.sub("", re.sub(r"\n+", " ", answer)).strip() or "…"
        entry = {"role": "assistant", "content": text}
        acts = self._compress_actions()
        if acts:
            entry["actions"] = acts
        self.memory["history"] += [{"role": "user", "content": user_text}, entry]
        self.save()
