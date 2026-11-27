from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import date, time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton
from unittest import mock

from tasks import TaskError, TaskStore, format_fields, format_slot, task_fields, task_line
from task_board import EMPTY_TEXT, TaskView
from task_views import TaskView as ExportedTaskView


class TaskUiTests(unittest.TestCase):
    day = date(2026, 11, 23)

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.cwd()))) / "tasks.db"

    def test_migration_preserves_existing_rows_and_related_state(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, deadline TEXT,
                    minutes INTEGER, status TEXT NOT NULL DEFAULT 'open', planned_day TEXT);
                INSERT INTO tasks VALUES (7, 'Draft', '2026-11-24', 20, 'open', '2026-11-23');
                INSERT INTO tasks VALUES (8, 'Sent', NULL, NULL, 'complete', NULL);
                CREATE TABLE mail_task_sources (source TEXT PRIMARY KEY, task_id INTEGER NOT NULL UNIQUE);
                INSERT INTO mail_task_sources VALUES ('fixture-source', 7);
                CREATE TABLE fired_reminders (task_id INTEGER NOT NULL, kind TEXT NOT NULL, scheduled TEXT NOT NULL,
                    PRIMARY KEY (task_id, kind, scheduled));
                INSERT INTO fired_reminders VALUES (7, 'planned', '2026-11-23');
            """)
        for _ in range(2):
            store = TaskStore(self.path)
            task, = store.open_tasks()
            self.assertEqual(task_line(task), "#7 Draft — 20 min | due 2026-11-24 | planned 2026-11-23")
            self.assertEqual(task_fields(task).scheduled, "2026-11-23（未排时段）")
            self.assertTrue(task.mail_linked)
            self.assertFalse(store.claim_reminder(task, "planned", "2026-11-23"))
            self.assertEqual(store.add_mail_action("fixture-source", "Duplicate", "", today=self.day), (7, False))
            self.assertEqual([(t.id, t.status) for t in store.list_tasks()], [(7, "open"), (8, "complete")])

    def test_slot_validation_edit_compatibility_and_display(self):
        store = TaskStore(self.path)
        with self.assertRaises(TaskError):
            store.add("Bad slot", "", "", "today", today=self.day, start="11:00", end="09:00")
        task_id = store.add("Notes", "", "30", "today", today=self.day, category="  课业  ",
                            start="09:00", end="09:30")
        store.edit(task_id, "tomorrow", "35", "today", today=self.day)
        task, = store.open_tasks()
        self.assertEqual(task_fields(task),
                         ("Notes", "课业", "35 分钟", "2026-11-23 09:00–09:30", "2026-11-24", "未完成"))
        store.schedule(task_id, "planned_day", "tomorrow", today=self.day)
        self.assertEqual(task_fields(store.open_tasks()[0]).scheduled, "2026-11-24（未排时段）")
        self.assertEqual(format_fields("课程", minutes=60).status, "不适用")
        self.assertEqual(format_slot(self.day, time(10), time(11), suggested=True), "建议：2026-11-23 10:00–11:00")

class TaskBoardTests(unittest.TestCase):
    day = date(2026, 11, 23)

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.cwd()))) / "tasks.db"
        self.store = TaskStore(self.path)
        self.events = []

    def view(self, store="default", error=""):
        view = TaskView(self.store if store == "default" else store, error)
        view.today = lambda: self.day
        view.changed.connect(lambda: self.events.append("changed"))
        self.addCleanup(view.close)
        self.addCleanup(view.editor_dialog.reject)
        view.refresh()
        return view

    def card(self, view, task_id):
        for column in view.columns.values():
            if task_id in column.cards:
                return column.cards[task_id]
        self.fail(f"task {task_id} is not shown")

    def add(self, title, category="", **fields):
        return self.store.add(title, fields.pop("deadline", ""), fields.pop("minutes", ""),
                              fields.pop("day", ""), today=self.day, category=category, **fields)

    def ids(self, view):
        return [task.id for task in view.visible_tasks()]

    def test_public_import_path_and_defaults(self):
        self.assertIs(ExportedTaskView, TaskView)
        view = self.view()
        self.assertEqual(view.mode, "open")
        self.assertTrue(view.mode_buttons["open"].isChecked())
        self.assertEqual([button.text() for button in view.mode_buttons.values()], ["未完成", "已完成", "全部"])
        self.assertEqual(view.new_button.text(), "＋ 新建待办")
        self.assertIsNone(view.editor)
        self.assertIsInstance(view.complete, QPushButton)
        self.assertFalse(view.complete.isEnabled())
        self.assertIsNone(view.selected_id())
        self.assertEqual(view.visible_tasks(), [])

    def test_empty_states(self):
        view = self.view()
        self.assertIs(view.main_stack.currentWidget(), view.empty)
        self.assertEqual(view.empty.text(), EMPTY_TEXT)
        self.store.add_category("甲")
        view.refresh()
        self.assertIs(view.main_stack.currentWidget(), view.board)
        self.assertEqual(view.visible_columns(), ["甲"])
        self.add("一件事", "甲")
        view.search.setText("没有这个")
        self.assertIs(view.main_stack.currentWidget(), view.empty)
        self.assertIn("没有符合条件", view.empty.text())
        view.search.clear()
        self.assertIs(view.main_stack.currentWidget(), view.board)

    def test_store_unavailable_shows_error_state(self):
        view = self.view(None, "本机待办数据库无法打开。")
        self.assertIs(view.main_stack.currentWidget(), view.empty)
        self.assertIn("本机待办数据库无法打开", view.empty.text())
        self.assertFalse(view.new_button.isEnabled())
        self.assertFalse(view.complete.isEnabled())
        self.assertFalse(view.select_task(1))
        self.assertEqual(view.visible_tasks(), [])
        self.assertIsNone(view.open_editor())
        view.refresh()
        self.assertEqual(view.columns, {})

    def test_inline_add_row_creates_task_in_its_column(self):
        self.store.add_category("工作")
        self.store.add_category("生活")
        view = self.view()
        column = view.columns["生活"]
        self.assertTrue(column.add_edit.isHidden())
        column.add_button.click()
        self.assertFalse(column.add_edit.isHidden())
        self.assertTrue(column.add_button.isHidden())
        column.add_edit.setText("  买牛奶  ")
        QTest.keyClick(column.add_edit, Qt.Key.Key_Return)
        task, = self.store.open_tasks()
        self.assertEqual((task.title, task.category, task.deadline, task.minutes), ("买牛奶", "生活", None, None))
        self.assertEqual(self.events, ["changed"])
        self.assertEqual(self.ids(view), [task.id])
        self.assertEqual(view.columns["生活"].cards[task.id].task.title, "买牛奶")
        self.assertFalse(view.columns["生活"].add_edit.isHidden())
        self.assertEqual(view.columns["生活"].add_edit.text(), "")
        self.assertIn("买牛奶", view.status.text())

    def test_inline_add_validation_error_and_escape(self):
        self.store.add_category("工作")
        view = self.view()
        view.columns["工作"].add_button.click()
        QTest.keyClick(view.columns["工作"].add_edit, Qt.Key.Key_Return)
        self.assertEqual(view.status.text(), "请输入任务名称。")
        self.assertTrue(view.status.property("error"))
        self.assertFalse(view.columns["工作"].add_edit.isHidden())
        self.assertEqual(self.store.list_tasks(), [])
        QTest.keyClick(view.columns["工作"].add_edit, Qt.Key.Key_Escape)
        self.assertTrue(view.columns["工作"].add_edit.isHidden())
        self.assertFalse(view.columns["工作"].add_button.isHidden())
        self.assertEqual(self.events, [])

    def test_inline_add_in_uncategorized_column(self):
        view = self.view()
        self.add("已有", "")
        view.refresh()
        view.columns[None].add_button.click()
        view.columns[None].add_edit.setText("新的")
        QTest.keyClick(view.columns[None].add_edit, Qt.Key.Key_Return)
        self.assertEqual([(t.title, t.category) for t in self.store.open_tasks()], [("已有", None), ("新的", None)])

    def test_dialog_creates_task_with_notes_and_new_typed_category(self):
        self.add("旧", "工作")
        view = self.view()
        view.new_button.click()
        editor = view.editor
        self.assertIsNotNone(editor)
        self.assertEqual(editor.windowTitle(), "新建待办")
        self.assertEqual(editor.width() >= 460 or editor.minimumWidth() >= 460, True)
        self.assertEqual([editor.category.itemText(i) for i in range(editor.category.count())], ["未分类", "工作"])
        for widget, name in ((editor.title, "标题"), (editor.notes, "备注"), (editor.category, "类别"),
                             (editor.minutes, "预估时长（分钟）"), (editor.day, "安排日期"),
                             (editor.start, "开始时间"), (editor.end, "结束时间"), (editor.deadline, "截止日期")):
            self.assertEqual(widget.accessibleName(), name)
        self.assertTrue(editor.delete.isHidden())
        self.assertTrue(editor.complete.isHidden())
        self.assertFalse(view.complete.isEnabled())
        editor.title.setText("写论文")
        editor.notes.setPlainText("第一章：引言\n第二章：方法")
        editor.category.setCurrentText("新项目")
        editor.minutes.setText("45")
        editor.day.setText("明天")
        editor.start.setText("09:00")
        editor.end.setText("10:00")
        editor.deadline.setText("2026-11-30 18:00")
        editor.accept()
        self.assertIsNone(view.editor)
        created = [t for t in self.store.list_tasks() if t.title == "写论文"][0]
        self.assertEqual((created.category, created.minutes, created.planned_day, created.start, created.end,
                          created.deadline), ("新项目", 45, "2026-11-24", "09:00", "10:00", "2026-11-30 18:00"))
        self.assertEqual(created.notes, "第一章：引言\n第二章：方法")
        self.assertEqual(self.store.categories(), ["工作", "新项目"])
        self.assertEqual(view.visible_columns(), ["工作", "新项目"])
        self.assertEqual(view.selected_id(), created.id)
        self.assertEqual(self.events, ["changed"])

    def test_dialog_defaults_to_uncategorized_and_prefills_single_visible_column(self):
        self.store.add_category("甲")
        self.store.add_category("乙")
        view = self.view()
        view.new_button.click()
        self.assertEqual(view.editor.category.currentText(), "未分类")
        view.editor.reject()
        view.category_rows["乙"].check.setChecked(False)
        view.new_button.click()
        self.assertEqual(view.editor.category.currentText(), "甲")
        view.editor.title.setText("放进甲")
        view.editor.category.setCurrentText("未分类")
        view.editor.accept()
        self.assertIsNone(self.store.open_tasks()[0].category)

    def test_dialog_edits_task_and_keeps_open_on_validation_error(self):
        task_id = self.add("原标题", "工作", notes="旧备注", minutes="30")
        self.store.add_category("个人")
        view = self.view()
        self.assertTrue(view.select_task(task_id))
        editor = view.editor
        self.assertEqual(editor.windowTitle(), "编辑待办")
        self.assertEqual((editor.title.text(), editor.notes.toPlainText(), editor.category.currentText(),
                          editor.minutes.text()), ("原标题", "旧备注", "工作", "30"))
        self.assertFalse(editor.delete.isHidden())
        editor.title.setText("")
        editor.accept()
        self.assertIs(view.editor, editor)
        self.assertEqual(editor.status.text(), "请输入任务名称。")
        editor.title.setText("新标题")
        editor.deadline.setText("不是日期")
        editor.accept()
        self.assertIs(view.editor, editor)
        self.assertIn("请输入有效日期", editor.status.text())
        self.assertEqual(self.store.list_tasks()[0].title, "原标题")
        editor.deadline.setText("2026-12-01")
        editor.notes.setPlainText("新备注")
        editor.category.setCurrentText("个人")
        editor.minutes.setText("")
        editor.accept()
        self.assertIsNone(view.editor)
        task, = self.store.list_tasks()
        self.assertEqual((task.title, task.notes, task.category, task.minutes, task.deadline),
                         ("新标题", "新备注", "个人", None, "2026-12-01"))
        self.assertEqual(self.events, ["changed"])
        self.assertEqual(view.selected_id(), task_id)

    def test_dialog_clears_uncategorized_and_deletes_with_confirmation(self):
        task_id = self.add("要删", "工作")
        view = self.view()
        view.select_task(task_id)
        view.editor.category.setCurrentText("未分类")
        view.editor.accept()
        self.assertIsNone(self.store.list_tasks()[0].category)
        view.select_task(task_id)
        asked = []
        view.confirm = lambda title, text, accept, parent=None: asked.append(text) or False
        view.editor.delete.click()
        self.assertIsNotNone(view.editor)
        self.assertEqual(len(self.store.list_tasks()), 1)
        view.confirm = lambda title, text, accept, parent=None: asked.append(text) or True
        view.editor.delete.click()
        self.assertIn("要删", asked[0])
        self.assertEqual(self.store.list_tasks(), [])
        self.assertIsNone(view.editor)
        self.assertIsNone(view.selected_id())
        self.assertEqual(self.events, ["changed", "changed"])

    def test_complete_and_reopen_from_card_check_button(self):
        task_id = self.add("检查", "工作")
        view = self.view()
        self.card(view, task_id).check.click()
        self.assertEqual(self.store.list_tasks()[0].status, "complete")
        self.assertEqual(view.visible_tasks(), [])
        view.mode_buttons["complete"].click()
        self.assertEqual(self.ids(view), [task_id])
        self.assertTrue(self.card(view, task_id).property("done"))
        self.assertEqual(self.card(view, task_id).check.accessibleName(), "重新打开：检查")
        self.card(view, task_id).check.click()
        self.assertEqual(self.store.list_tasks()[0].status, "open")
        self.assertEqual(self.store.list_tasks()[0].completed_at, None)
        self.assertEqual(self.events, ["changed", "changed"])

    def test_complete_button_follows_editor_and_task_state(self):
        open_id = self.add("进行中", "工作")
        done_id = self.add("做完了", "工作")
        self.store.complete(done_id)
        view = self.view()
        self.assertFalse(view.complete.isEnabled())
        view.select_task(open_id)
        self.assertTrue(view.complete.isEnabled())
        self.assertEqual(view.complete.text(), "标为完成")
        view.editor.reject()
        self.assertFalse(view.complete.isEnabled())
        view.select_task(done_id)
        self.assertFalse(view.complete.isEnabled())
        self.assertFalse(view.editor.reopen.isHidden())
        self.assertTrue(view.editor.complete.isHidden())
        view.editor.reopen.click()
        self.assertEqual(self.store.list_tasks()[1].status, "open")
        self.assertIsNone(view.editor)
        view.select_task(open_id)
        view.complete.click()
        self.assertEqual(self.store.list_tasks()[0].status, "complete")
        self.assertIsNone(view.editor)
        self.assertFalse(view.complete.isEnabled())
        self.assertEqual(self.events, ["changed", "changed"])
        view.complete.click()
        self.assertEqual(self.events, ["changed", "changed"])

    def test_completion_modes_and_collapsible_completed_section(self):
        first = self.add("甲一", "工作")
        second = self.add("甲二", "工作")
        done = self.add("甲完成", "工作")
        other = self.add("乙完成", "个人")
        self.store.complete(done)
        self.store.complete(other)
        view = self.view()
        self.assertEqual(self.ids(view), [first, second])
        toggle = view.columns["工作"].done_toggle
        self.assertIn("已完成 (1)", toggle.text())
        self.assertIn("已完成 (1)", view.columns["个人"].done_toggle.text())
        toggle.click()
        self.assertEqual(self.ids(view), [first, second, done])
        view.mode_buttons["complete"].click()
        self.assertEqual(view.mode, "complete")
        self.assertEqual(self.ids(view), [done, other])
        self.assertTrue(all(task.status == "complete" for task in view.visible_tasks()))
        self.assertIsNone(view.columns["工作"].done_toggle)
        self.assertTrue(view.columns["工作"].add_button.isHidden())
        view.mode_buttons["all"].click()
        self.assertEqual(self.ids(view), [first, second, done, other])
        self.assertTrue(view.mode_buttons["all"].isChecked())
        view.mode_buttons["open"].click()
        self.assertEqual(self.ids(view), [first, second, done])

    def test_category_columns_can_be_hidden_and_restored(self):
        self.store.add_category("甲")
        self.store.add_category("乙")
        a, b, loose = self.add("A", "甲"), self.add("B", "乙"), self.add("C", "")
        view = self.view()
        self.assertEqual(view.visible_columns(), ["甲", "乙", None])
        self.assertTrue(view.all_button.isChecked())
        self.assertEqual(view.category_rows["甲"].count.text(), "1")
        view.category_rows["乙"].check.setChecked(False)
        self.assertEqual(view.visible_columns(), ["甲", None])
        self.assertEqual(self.ids(view), [a, loose])
        self.assertFalse(view.all_button.isChecked())
        view.category_rows["甲"].check.setChecked(False)
        self.assertEqual(view.visible_columns(), [None])
        view.category_rows[None].check.setChecked(False)
        self.assertEqual(view.visible_columns(), [None])
        self.assertTrue(view.category_rows[None].check.isChecked())
        self.assertIn("至少", view.status.text())
        view.all_button.click()
        self.assertEqual(view.visible_columns(), ["甲", "乙", None])
        self.assertEqual(self.ids(view), [a, b, loose])
        self.assertTrue(all(row.check.isChecked() for row in view.category_rows.values()))

    def test_uncategorized_column_only_when_needed(self):
        view = self.view()
        self.assertEqual(view.visible_columns(), [])
        self.store.add_category("甲")
        view.refresh()
        self.assertEqual(view.visible_columns(), ["甲"])
        self.add("无类别")
        view.refresh()
        self.assertEqual(view.visible_columns(), ["甲", None])
        self.assertEqual(list(view.category_rows), ["甲", None])
        self.assertIsNone(view.category_rows[None].menu_button)

    def test_refresh_picks_up_assistant_changes_to_categories(self):
        self.store.add_category("甲")
        self.store.add_category("乙")
        a, b = self.add("A", "甲"), self.add("B", "乙")
        view = self.view()
        view.select_task(a)
        view.editor.reject()
        view.category_rows["乙"].check.setChecked(False)
        self.store.rename_category("甲", "丙")
        self.store.add_category("新类别")
        view.refresh()
        self.assertEqual(view.visible_columns(), ["丙", "新类别"])
        self.assertEqual(view.selected_id(), a)
        self.assertEqual(view.columns["丙"].cards[a].task.category, "丙")
        self.store.delete_category("丙")
        self.store.delete_category("乙")
        view.refresh()
        self.assertEqual(view.visible_columns(), ["新类别", None])
        self.assertEqual(self.ids(view), [a, b])
        self.store.delete_category("新类别")
        view.refresh()
        self.assertEqual(view.visible_columns(), [None])
        self.assertEqual(self.events, [])

    def test_refresh_survives_all_visible_categories_vanishing(self):
        self.store.add_category("甲")
        self.store.add_category("乙")
        self.add("A", "甲")
        view = self.view()
        view.category_rows["乙"].check.setChecked(False)
        self.assertEqual(view.visible_columns(), ["甲"])
        self.store.delete_category("甲")
        view.refresh()
        self.assertTrue(view.visible_columns())

    def test_rename_and_delete_category_from_the_view(self):
        self.store.add_category("甲")
        task_id = self.add("A", "甲")
        view = self.view()
        self.assertEqual([a.text() for a in view.category_rows["甲"].menu_button.menu().actions()],
                         ["重命名", "删除类别"])
        view.category_rows["甲"].check.setChecked(True)
        view.ask_name = lambda *args: None
        self.assertFalse(view.rename_category("甲"))
        view.ask_name = lambda title, label, text="": "改名后"
        self.assertTrue(view.rename_category("甲"))
        self.assertEqual(self.store.categories(), ["改名后"])
        self.assertEqual(view.columns["改名后"].cards[task_id].task.category, "改名后")
        self.assertEqual(self.events, ["changed"])
        view.ask_name = lambda title, label, text="": "未分类"
        self.assertFalse(view.rename_category("改名后"))
        self.assertTrue(view.status.property("error"))
        view.ask_name = lambda title, label, text="": "   "
        self.assertFalse(view.rename_category("改名后"))
        self.assertEqual(view.status.text(), "请输入新的类别名称。")
        shown = []
        view.confirm = lambda title, text, accept, parent=None: shown.append(text) or False
        self.assertFalse(view.delete_category("改名后"))
        self.assertEqual(self.store.categories(), ["改名后"])
        self.assertIn("未分类", shown[0])
        self.assertIn("1 条", shown[0])
        view.confirm = lambda title, text, accept, parent=None: True
        self.assertTrue(view.delete_category("改名后"))
        self.assertEqual(self.store.categories(), [])
        self.assertIsNone(self.store.list_tasks()[0].category)
        self.assertEqual(view.visible_columns(), [None])
        self.assertEqual(self.events, ["changed", "changed"])

    def test_create_category_from_sidebar(self):
        view = self.view()
        view.ask_name = lambda title, label, text="": "项目 abc"
        view.new_category.click()
        self.assertEqual(self.store.categories(), ["项目 abc"])
        self.assertEqual(view.visible_columns(), ["项目 abc"])
        self.assertEqual(self.events, ["changed"])
        self.assertTrue(view.create_category("项目 ABC"))
        self.assertEqual(self.store.categories(), ["项目 abc"])
        self.assertIn("已经存在", view.status.text())
        self.assertEqual(self.events, ["changed"])
        self.assertFalse(view.create_category("未分类"))
        self.assertEqual(self.store.categories(), ["项目 abc"])
        view.ask_name = lambda title, label, text="": None
        view.new_category.click()
        self.assertEqual(self.store.categories(), ["项目 abc"])

    def test_search_filters_cards_by_title(self):
        a = self.add("写周报", "工作")
        b = self.add("买菜", "生活")
        c = self.add("周末聚餐", "生活")
        self.store.complete(c)
        view = self.view()
        view.search.setText("周")
        self.assertEqual(self.ids(view), [a])
        view.mode_buttons["all"].click()
        self.assertEqual(self.ids(view), [a, c])
        view.search.setText("买")
        self.assertEqual(self.ids(view), [b])
        view.search.setText("")
        self.assertEqual(self.ids(view), [a, b, c])

    def test_select_task_reveals_hidden_completed_task_and_opens_editor(self):
        self.store.add_category("甲")
        self.store.add_category("乙")
        a = self.add("甲任务", "甲")
        b = self.add("乙已完成", "乙")
        self.store.complete(b)
        view = self.view()
        view.category_rows["乙"].check.setChecked(False)
        view.search.setText("甲")
        self.assertEqual(self.ids(view), [a])
        self.assertTrue(view.select_task(b))
        self.assertEqual(view.search.text(), "")
        self.assertEqual(view.mode, "all")
        self.assertTrue(view.mode_buttons["all"].isChecked())
        self.assertEqual(view.visible_columns(), ["甲", "乙"])
        self.assertIn(b, self.ids(view))
        self.assertEqual(view.selected_id(), b)
        self.assertEqual(view.editor.task_id, b)
        self.assertTrue(self.card(view, b).property("selected"))
        self.assertFalse(self.card(view, a).property("selected"))
        view.editor.reject()
        view.mode_buttons["complete"].click()
        self.assertTrue(view.select_task(a))
        self.assertEqual(view.mode, "all")
        view.editor.reject()
        self.assertTrue(view.select_task(a))
        self.assertEqual(view.mode, "all")

    def test_select_task_keeps_filters_that_do_not_hide_it_and_rejects_unknown_ids(self):
        a = self.add("甲", "工作")
        self.add("乙", "个人")
        view = self.view()
        view.category_rows["个人"].check.setChecked(False)
        view.search.setText("甲")
        self.assertTrue(view.select_task(a))
        self.assertEqual((view.mode, view.search.text(), view.visible_columns()), ("open", "甲", ["工作"]))
        view.editor.reject()
        self.assertFalse(view.select_task(999))
        self.assertEqual(view.selected_id(), a)
        self.assertIsNone(view.editor)

    def test_selection_survives_refresh_and_disappears_with_task(self):
        a = self.add("甲", "工作")
        view = self.view()
        view.select_task(a)
        view.editor.reject()
        view.refresh()
        self.assertEqual(view.selected_id(), a)
        self.assertTrue(self.card(view, a).property("selected"))
        self.store.delete(a)
        view.refresh()
        self.assertIsNone(view.selected_id())

    def test_task_errors_are_shown_in_status_label(self):
        task_id = self.add("甲", "工作")
        view = self.view()
        message = "这条待办已不是未完成状态，请刷新后再试。"
        with mock.patch.object(self.store, "complete", side_effect=TaskError(message)):
            self.card(view, task_id).check.click()
        self.assertEqual(view.status.text(), message)
        self.assertTrue(view.status.property("error"))
        self.assertEqual(self.events, [])
        database = "本机待办数据库读取失败。请检查访问权限后重试。"
        with mock.patch.object(self.store, "list_tasks", side_effect=TaskError(database)):
            view.refresh()
        self.assertIn("无法读取或更新本机待办", view.status.text())
        self.assertEqual(view.status.toolTip(), database)
        self.assertEqual(self.ids(view), [task_id])
        view.refresh()
        self.assertEqual(view.status.text(), "")

    def test_dialog_shows_store_errors_without_closing(self):
        task_id = self.add("甲", "工作")
        view = self.view()
        view.select_task(task_id)
        with mock.patch.object(self.store, "edit_details", side_effect=TaskError("本机待办数据库更新失败。请检查访问权限后重试。")):
            view.editor.accept()
        self.assertIsNotNone(view.editor)
        self.assertIn("无法读取或更新本机待办", view.editor.status.text())
        self.store.delete(task_id)
        view.editor.accept()
        self.assertEqual(view.editor.status.text(), "这条待办已不存在。")

    def test_cards_follow_mandatory_field_order_and_flag_overdue_and_mail(self):
        late = self.add("过期", "工作", deadline="2026-11-20", minutes="45")
        slotted = self.add("排好", "工作", day="2026-11-23", minutes="30", start="09:00", end="09:30")
        mail, _ = self.store.add_mail_action("source", "邮件待办", "2026-11-25", today=self.day)
        done = self.add("完成", "工作", deadline="2026-11-01")
        self.store.complete(done)
        view = self.view()
        view.mode_buttons["all"].click()

        def plain(label):
            return label.text().replace("\u2060", "").replace("\u00a0", " ")

        late_card = self.card(view, late)
        self.assertIn("工作｜45 分钟｜未安排｜", plain(late_card.caption))
        self.assertIn("2026-11-20", plain(late_card.caption))
        self.assertIn("｜未完成", plain(late_card.caption))
        self.assertIn("#ff8f8f", late_card.caption.text())
        self.assertNotIn("过期", plain(late_card.caption))
        self.assertEqual(late_card.accessibleName(), task_fields(view.records[late]).line())
        self.assertIsNone(late_card.chip)
        self.assertIn("09:00–09:30", plain(self.card(view, slotted).caption))
        self.assertNotIn("#ff8f8f", self.card(view, slotted).caption.text())
        self.assertEqual(self.card(view, mail).chip.text(), "来自邮件")
        done_card = self.card(view, done)
        self.assertNotIn("#ff8f8f", done_card.caption.text())
        self.assertTrue(done_card.title.property("done"))
        self.assertIn("已完成", plain(done_card.caption))

    def test_cards_are_ordered_by_deadline_then_creation(self):
        none_first = self.add("无期限", "工作")
        later = self.add("较晚", "工作", deadline="2026-12-01")
        sooner = self.add("较早", "工作", deadline="2026-11-24")
        view = self.view()
        self.assertEqual(self.ids(view), [sooner, later, none_first])

    def test_keyboard_enter_opens_card_and_space_completes(self):
        task_id = self.add("键盘", "工作")
        view = self.view()
        card = self.card(view, task_id)
        self.assertEqual(card.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        self.assertEqual(card.check.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        QTest.keyClick(card, Qt.Key.Key_Delete)
        self.assertEqual(len(self.store.list_tasks()), 1)
        self.assertIsNone(view.editor)
        QTest.keyClick(card, Qt.Key.Key_Return)
        self.assertEqual(view.editor.task_id, task_id)
        view.editor.reject()
        QTest.keyClick(self.card(view, task_id).check, Qt.Key.Key_Space)
        self.assertEqual(self.store.list_tasks()[0].status, "complete")

    def test_tab_order_runs_header_sidebar_then_columns(self):
        self.store.add_category("甲")
        first = self.add("一", "甲")
        second = self.add("二", "甲")
        view = self.view()
        chain = [*view.mode_buttons.values(), view.search, view.new_button, view.all_button,
                 view.category_rows["甲"].check, view.category_rows["甲"].menu_button, view.new_category]
        def tab_next(widget):
            following = widget.nextInFocusChain()
            while not following.focusPolicy() & Qt.FocusPolicy.TabFocus:
                following = following.nextInFocusChain()
            return following

        for before, after in zip(chain, chain[1:]):
            self.assertIs(tab_next(before), after)
        column = view.columns["甲"]
        self.assertIs(tab_next(view.new_category), column.menu_button)
        self.assertIs(tab_next(column.menu_button), column.add_button)
        self.assertIs(tab_next(column.add_button), column.add_edit)
        self.assertIs(tab_next(column.add_edit), column.cards[first])
        self.assertIs(tab_next(column.cards[first]), column.cards[first].check)
        self.assertIs(tab_next(column.cards[first].check), column.cards[second])

    def test_clicking_a_card_selects_it_and_opens_the_editor(self):
        task_id = self.add("点击", "工作")
        view = self.view()
        QTest.mouseClick(self.card(view, task_id), Qt.MouseButton.LeftButton)
        self.assertEqual(view.selected_id(), task_id)
        self.assertEqual(view.editor.task_id, task_id)

    def test_focus_primary_prefers_first_card_then_new_button(self):
        view = self.view()
        view.show()
        view.focus_primary()
        self.assertIs(view.focusWidget(), view.new_button)
        task_id = self.add("甲", "工作")
        view.refresh()
        view.focus_primary()
        self.assertIs(view.focusWidget(), self.card(view, task_id))

    def test_focus_stays_on_board_after_completing_with_keyboard(self):
        first, second = self.add("甲", "工作"), self.add("乙", "工作")
        view = self.view()
        view.resize(1100, 680)
        view.show()
        QTest.qWait(50)
        self.card(view, first).check.setFocus()
        QTest.keyClick(self.card(view, first).check, Qt.Key.Key_Space)
        self.assertEqual(self.ids(view), [second])
        self.assertIs(view.focusWidget(), self.card(view, second))

    def test_refresh_preserves_scroll_and_inline_text(self):
        self.store.add_category("长")
        for number in range(30):
            self.add(f"任务 {number}", "长")
        view = self.view()
        view.resize(1100, 520)
        view.show()
        QTest.qWait(100)
        bar = view.columns["长"].scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum() // 2)
        wanted = bar.value()
        view.columns["长"].add_button.click()
        view.columns["长"].add_edit.setText("草稿")
        view.refresh()
        QTest.qWait(50)
        self.assertEqual(view.columns["长"].scroll.verticalScrollBar().value(), wanted)
        self.assertEqual(view.columns["长"].add_edit.text(), "草稿")
        self.assertFalse(view.columns["长"].add_edit.isHidden())

    def test_columns_share_width_within_limits_and_only_board_scrolls_sideways(self):
        for name in ("甲", "乙"):
            self.store.add_category(name)
        view = self.view()
        view.resize(1100, 680)
        view.show()
        QTest.qWait(100)
        widths = [column.width() for column in view.columns.values()]
        self.assertTrue(all(280 <= width <= 360 for width in widths), widths)
        self.assertEqual(view.board.horizontalScrollBar().maximum(), 0)
        view.category_rows["乙"].check.setChecked(False)
        self.assertEqual(view.columns["甲"].width(), 360)
        for number in range(6):
            self.store.add_category(f"类别 {number}")
        view.refresh()
        view.all_button.click()
        QTest.qWait(100)
        self.assertTrue(all(column.width() == 280 for column in view.columns.values()))
        self.assertGreater(view.board.horizontalScrollBar().maximum(), 0)
        self.assertEqual(view.side_scroll.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        column = next(iter(view.columns.values()))
        self.assertEqual(column.scroll.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def test_long_titles_wrap_inside_the_card(self):
        long_title = "非常长的待办标题" * 8 + "x" * 60
        task_id = self.add(long_title, "工作")
        view = self.view()
        view.resize(1100, 680)
        view.show()
        QTest.qWait(100)
        card = self.card(view, task_id)
        viewport = view.columns["工作"].scroll.viewport()
        self.assertLessEqual(card.width(), viewport.width())
        self.assertGreater(card.title.height(), card.caption.height())
        self.assertEqual(view.columns["工作"].scroll.horizontalScrollBar().maximum(), 0)
        self.assertEqual(card.accessibleName().split("｜")[0], long_title)
