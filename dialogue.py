from dataclasses import dataclass
from typing import Literal

Expression = Literal["neutral", "smile", "focused"]


@dataclass(frozen=True)
class Reply:
    reply: str
    expression: Expression


def respond(message: str) -> Reply:
    text = message.casefold()
    if any(word in text for word in ("待办", "计划", "做什么", "schedule", "to-do", "today")):
        if text.isascii():
            return Reply("List your tasks and deadlines. We'll decide what comes first.", "focused")
        return Reply("请先列出你的待办和截止时间。我们再决定先做哪一件。", "focused")
    if any(word in text for word in ("早上好", "早安", "good morning")):
        if text.isascii():
            return Reply("Good morning. What would you like to start with?", "smile")
        return Reply("早上好。今天想先从哪件事开始？", "smile")
    if text.isascii():
        return Reply("Tell me what you'd like to get done, and we'll pick a next step.", "neutral")
    return Reply("请告诉我你想完成什么，我们一起确定下一步。", "neutral")
