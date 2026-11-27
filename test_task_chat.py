from contextlib import closing
from datetime import date, datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from dialogue import Reply
from task_chat import (
    Operation, Proposal, TaskAction, TaskReply, apply_task_reply, approve, task_context,
)
from tasks import TaskError, TaskStore

NOW = datetime(2026, 11, 22, 16, 50)


def reply(mode: str, *operations: Operation, text: str = "好的。") -> TaskReply:
    return TaskReply(text, "focused", TaskAction(mode, tuple(operations)))


class StoreCategoryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "tasks.sqlite3"
        self.store = TaskStore(self.path)

    def test_categories_exist_without_tasks_and_follow_renames_and_deletes(self) -> None:
        self.assertEqual(self.store.add_category(" Project   ABC "), "Project ABC")
        self.assertEqual(self.store.add_category("project abc"), "Project ABC")
        first = self.store.add("Draft", "", "", "", today=NOW.date(), category="project abc")
        second = self.store.add("Other", "", "", "", today=NOW.date(), category="CSC")
        self.assertEqual(self.store.categories(), ["Project ABC", "CSC"])
        self.assertEqual({task.id: task.category for task in self.store.list_tasks()},
                         {first: "Project ABC", second: "CSC"})
        self.assertEqual(self.store.rename_category("CSC", "CSC 301"), "CSC 301")
        self.assertEqual(self.store.list_tasks()[1].category, "CSC 301")
        with self.assertRaises(TaskError):
            self.store.rename_category("CSC 301", "project abc")
        self.assertEqual(self.store.delete_category("Project ABC"), 1)
        self.assertIsNone(self.store.list_tasks()[0].category)
        self.assertEqual(self.store.categories(), ["CSC 301"])

    def test_reserved_uncategorized_label_is_not_a_real_category(self) -> None:
        with self.assertRaises(TaskError):
            self.store.add_category(" 未分类 ")
        task_id = self.store.add("Loose", "", "", "", today=NOW.date(), category="未分类")
        self.assertIsNone(self.store.list_tasks()[0].category)
        self.assertEqual(self.store.categories(), [])
        self.store.add_category("Work")
        with self.assertRaises(TaskError):
            self.store.rename_category("Work", "未分类")
        self.assertIsNone(self.store.set_category(task_id, "未分类"))

    def test_notes_reopen_delete_restore_and_mail_dismissal(self) -> None:
        task_id = self.store.add("Write", "", "", "", today=NOW.date(), notes=" outline ")
        before = self.store.list_tasks()[0]
        self.assertEqual(before.notes, "outline")
        self.store.edit_details(task_id, "Write more", "", "", "", today=NOW.date())
        self.assertEqual(self.store.list_tasks()[0].notes, "outline")
        self.store.complete(task_id)
        self.assertIsNotNone(self.store.list_tasks()[0].completed_at)
        self.store.reopen(task_id)
        self.assertEqual(self.store.list_tasks()[0].status, "open")
        self.store.delete(task_id)
        self.assertEqual(self.store.list_tasks(), [])
        self.store.restore(before)
        self.assertEqual(self.store.list_tasks()[0].title, "Write")
        self.store.dismiss_mail("a", now=NOW)
        self.store.dismiss_mail("b", now=NOW)
        self.assertEqual(self.store.dismissed_mail(), {"a", "b"})
        self.store.restore_mail("a")
        self.assertEqual(self.store.dismissed_mail(), {"b"})
        self.store.clear_dismissed_mail()
        self.assertEqual(self.store.dismissed_mail(), set())

    def test_existing_database_is_migrated_without_losing_tasks_or_categories(self) -> None:
        legacy = Path(self.path.parent) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute("""CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, deadline TEXT,
                minutes INTEGER, status TEXT NOT NULL DEFAULT 'open', planned_day TEXT, category TEXT)""")
            db.execute("INSERT INTO tasks (title, deadline, minutes, status, category) "
                       "VALUES ('Old', '2026-11-30', 20, 'complete', 'Courses')")
        for _ in range(2):
            store = TaskStore(legacy)
        task, = store.list_tasks()
        self.assertEqual((task.title, task.deadline, task.minutes, task.status, task.category),
                         ("Old", "2026-11-30", 20, "complete", "Courses"))
        self.assertEqual(store.categories(), ["Courses"])


class TaskChatTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TaskStore(Path(directory.name) / "tasks.sqlite3")

    def titles(self) -> list[str]:
        return [task.title for task in self.store.open_tasks()]

    def test_the_message_wording_never_gates_a_model_decision(self) -> None:
        plans = tuple(Operation("add", title=title) for title in ("写简历", "投三个岗位", "跑步"))
        for message in ("你帮我把这三件事放入代办吧", "嗯，就这样吧", "sure, put those in", "!!!"):
            with self.subTest(message=message):
                outcome = apply_task_reply(reply("execute", *plans), message, self.store, NOW)
                self.assertTrue(outcome.changed)
                self.assertRegex(outcome.reply.reply, "已新增|Added")
                for task in self.store.open_tasks():
                    self.store.delete(task.id)

    def test_proposal_waits_then_approval_writes_exactly_the_staged_changes(self) -> None:
        plans = (Operation("add", title="写简历", date="2026-11-22", field="planned_day"),
                 Operation("add", title="跑步", minutes="30", category="Health"))
        proposed = apply_task_reply(reply("propose", *plans, text="要放进待办吗？"), "今天还要写简历、跑步", self.store, NOW)
        self.assertEqual(self.titles(), [])
        self.assertIsNotNone(proposed.proposal)
        assert proposed.proposal is not None
        self.assertIn("要放进待办吗？", proposed.reply.reply)
        self.assertIn("写简历", proposed.reply.reply)
        approved = apply_task_reply(reply("approve_pending", text="ignored"), "好呀", self.store, NOW,
                                    pending=proposed.proposal)
        self.assertTrue(approved.changed)
        self.assertEqual(self.titles(), ["写简历", "跑步"])
        self.assertEqual(self.store.categories(), ["Health"])
        self.assertEqual(self.store.list_tasks()[0].planned_day, "2026-11-22")
        self.assertIsNone(apply_task_reply(reply("cancel_pending"), "不用了", self.store, NOW,
                                           pending=proposed.proposal).proposal)
        none = apply_task_reply(reply("approve_pending"), "好", self.store, NOW)
        self.assertFalse(none.changed)
        self.assertIn("没有待确认", none.reply.reply)

    def test_failures_are_reported_per_operation_and_drop_the_model_claim(self) -> None:
        self.store.add("Keep", "", "", "", today=NOW.date())
        outcome = apply_task_reply(
            reply("execute", Operation("add", title="Good"), Operation("complete", target="#99"),
                  Operation("add", title="Bad", minutes="0"), text="全部搞定了"),
            "do it", self.store, NOW)
        self.assertTrue(outcome.changed)
        self.assertEqual(self.titles(), ["Keep", "Good"])
        self.assertNotIn("全部搞定了", outcome.reply.reply)
        self.assertIn("Not changed", outcome.reply.reply)
        self.assertIn("Added", outcome.reply.reply)

    def test_category_operations_and_destructive_ones_always_ask_first(self) -> None:
        task_id = self.store.add("复习", "", "", "", today=NOW.date())
        apply_task_reply(reply("execute", Operation("create_category", category="Project ABC")), "x", self.store, NOW)
        apply_task_reply(reply("execute", Operation("set_category", target=f"#{task_id}", category="project abc")),
                         "x", self.store, NOW)
        self.assertEqual(self.store.list_tasks()[0].category, "Project ABC")
        apply_task_reply(reply("execute", Operation("rename_category", category="Project ABC", new_name="ABC")),
                         "x", self.store, NOW)
        self.assertEqual((self.store.categories(), self.store.list_tasks()[0].category), (["ABC"], "ABC"))
        staged = apply_task_reply(reply("execute", Operation("delete_category", category="abc")), "x", self.store, NOW)
        self.assertEqual(self.store.categories(), ["ABC"])
        assert staged.proposal is not None
        approve(staged.proposal, self.store, NOW, True)
        self.assertEqual((self.store.categories(), self.store.list_tasks()[0].category), ([], None))

    def test_update_reschedule_complete_and_undo(self) -> None:
        task_id = self.store.add("报告", "2026-11-25", "30", "2026-11-22", today=NOW.date(), start="09:00", end="09:30")
        target = f"#{task_id}"
        moved = apply_task_reply(reply("execute", Operation("reschedule", target=target, field="planned_day",
                                                            date="2026-11-23", start="10:00", end="10:30")),
                                 "x", self.store, NOW)
        task = self.store.list_tasks()[0]
        self.assertEqual((task.planned_day, task.start, task.end, task.deadline),
                         ("2026-11-23", "10:00", "10:30", "2026-11-25"))
        renamed = apply_task_reply(reply("execute", Operation("update", target="报告", new_name="期末报告", notes="大纲")),
                                   "x", self.store, NOW)
        task = self.store.list_tasks()[0]
        self.assertEqual((task.title, task.notes, task.start), ("期末报告", "大纲", "10:00"))
        done = apply_task_reply(reply("execute", Operation("complete", target=target)), "x", self.store, NOW)
        self.assertEqual(self.store.list_tasks()[0].status, "complete")
        undone = apply_task_reply(reply("execute", Operation("undo")), "撤销", self.store, NOW, last_undo=done.undo)
        self.assertTrue(undone.undone)
        self.assertEqual(self.store.list_tasks()[0].status, "open")
        apply_task_reply(reply("execute", Operation("undo")), "撤销", self.store, NOW, last_undo=renamed.undo)
        self.assertEqual(self.store.list_tasks()[0].title, "报告")
        apply_task_reply(reply("execute", Operation("undo")), "撤销", self.store, NOW, last_undo=moved.undo)
        self.assertEqual(self.store.list_tasks()[0].planned_day, "2026-11-22")
        nothing = apply_task_reply(reply("execute", Operation("undo")), "undo", self.store, NOW)
        self.assertIn("no change", nothing.reply.reply.lower())

    def test_read_only_operations_and_plain_chat_do_not_write(self) -> None:
        self.store.add("报告", "", "30", "", today=NOW.date())
        listing = apply_task_reply(reply("none", Operation("list")), "有哪些待办", self.store, NOW)
        self.assertIn("报告", listing.reply.reply)
        plan = apply_task_reply(reply("none", Operation("plan", minutes="60")), "我有一小时", self.store, NOW)
        self.assertIn("30 / 60", plan.reply.reply)
        chat = apply_task_reply(Reply("你好。", "smile"), "你好", self.store, NOW)
        self.assertEqual((chat.reply.reply, chat.changed, chat.proposal), ("你好。", False, None))
        casual = apply_task_reply(reply("none"), "我又忘了吃午饭", self.store, NOW)
        self.assertEqual(self.titles(), ["报告"])
        self.assertFalse(casual.changed)

    def test_model_context_has_minimum_task_data_and_no_mail_titles_or_notes(self) -> None:
        self.store.add("复习", "2026-11-25", "30", "", today=NOW.date(), category="CSC", notes="secret notes")
        self.store.add_mail_action("src", "面试邀请：Private Corp", "", today=NOW.date())
        pending = Proposal((Operation("add", title="跑步", task_id=None),))
        context = task_context(self.store, NOW, pending)
        self.assertIn("复习", context)
        self.assertIn("CSC", context)
        self.assertIn("pending_proposal", context)
        self.assertNotIn("secret notes", context)
        self.assertNotIn("Private Corp", context)
        self.assertIn("2026-11-22", context)

    def test_invalid_model_payloads_are_rejected(self) -> None:
        with self.assertRaises(TaskError):
            TaskAction.parse({"mode": "execute"})
        with self.assertRaises(TaskError):
            TaskAction.parse({"mode": "yolo", "operations": []})
        with self.assertRaises(TaskError):
            TaskAction.parse({"mode": "execute", "operations": [{"kind": "add"}]})


if __name__ == "__main__":
    unittest.main()
