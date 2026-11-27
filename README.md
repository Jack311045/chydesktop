# Chihaya Desktop

A native Windows desktop secretary built with Python and PySide6 Qt Widgets. It starts in Offline mode. Claude mode is optional, and chat history is not saved to disk.

## Run from source on Windows

From the project root in PowerShell, with 64-bit Python 3.12 installed:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\chihaya.py
```

## Build and run the portable app

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath .\dist\Office Chihaya.spec
```

Extract `dist\Office\Chihaya-Portable.zip` or open `dist\Office\Chihaya` in File Explorer and double-click `Chihaya.exe`. Keep the entire `Chihaya` folder together, including `Chihaya.exe` and its sibling `_internal` directory. Move or copy the full folder, not just the EXE. The portable app needs no Python installation. Close the executable before rebuilding into the same folder. The ZIP does not contain your credentials, mail, tasks, settings, portraits, or reference screenshots.

## Office home

The interface is in Simplified Chinese with a dark night theme. The five pages are `事务所`, `今日`, `待办`, `信箱`, and `设置`. Chat is part of the Office; collapse the transcript without clearing your draft. Claude is the default reply mode when `%LOCALAPPDATA%\Chihaya\.env` holds a valid `ANTHROPIC_API_KEY`; otherwise the app starts in `离线` and the chat panel explains what is missing (the key is never displayed). Use `设置` for Google Calendar and Gmail connections, included calendars, job keywords, the reply mode, reminders, and tray behavior. Old task databases are upgraded in place; a date without a time remains unscheduled within that date. Planner suggestions are not saved.

`今日` is a Google-Calendar-style page: a mini month and calendar list on the left, a Sunday-first week (or single day) time grid with a current-time line, and an event detail card. Use `今天`, the arrows, the date box, or `日`/`周` to move around, and `简报与规划` to open the briefing, due tasks, and planner panel. The week is read with one authorized session and one events query per included calendar. Calendars stay read-only.

The monitor on the right desk shows a mail icon and opens `信箱`; Chihaya, the whiteboard and the monitor are label-free hotspots. Hovering or focusing Chihaya traces her own silhouette (taken from the alpha channel of the current portrait PNG) and only that shape is clickable. `信箱` gives the unread list and its text-only preview (never marked as read) most of the page. Below them two parallel tabs, `待跟进` and `待确认`, hold the Daily Brief mail items; `预览原信` shows the original in the preview above. `标为完成` removes an item you already handled without adding it to the to-do list; it stays hidden on later scans until a newer message arrives, and `恢复已完成` brings the hidden ones back. Nothing is changed in Gmail.

Every page uses one font: Noto Sans SC Medium (9.5 pt), with Microsoft YaHei UI and Segoe UI as fallbacks on machines without it. Huninn does not contain many Simplified glyphs (29 of 65 sampled characters were missing), so it is bundled only as a last fallback together with its SIL Open Font License in [assets/fonts](./assets/fonts).

### Local screenshot OCR on Windows

1. Install Tesseract using the [Windows installer](https://github.com/UB-Mannheim/tesseract/wiki) or run `winget install --id UB-Mannheim.TesseractOCR --exact`. The app looks in the usual Program Files location and otherwise uses `tesseract` on `PATH`.
2. Install `eng.traineddata` plus `chi_sim.traineddata` and/or `chi_tra.traineddata` in Tesseract's `tessdata` directory. The [Tesseract language data repository](https://github.com/tesseract-ocr/tessdata_fast) supplies these files. The app uses whichever of the three are installed (`eng+chi_sim+chi_tra` when all are present) and falls back to `eng`. Verify with `tesseract --list-langs`. The OCR executable and language files are separate from the Chihaya package.
3. Paste an image into the Office input with Ctrl+V or choose `选择截图` (the standard Windows file picker). Compare the editable OCR text with the original, especially the date, time range, and deadline. `带入对话` fills the draft without sending it. `编辑待办草稿` stages one task title for separate confirmation. Missing OCR, an empty result, and a 15-second timeout leave the review text editable.

Open Calendar from the whiteboard, Chat from Chihaya, or unread Gmail from the monitor. The cards use locally computed openings, existing Calendar results, and local tasks. `今日到期` includes overdue open tasks but excludes tasks merely planned for today. `待回复` counts loaded follow-up actions, including qualifying read mail; Review and status items are separate. These are not inbox totals.

Use Tab to reach a card or shortcut and Enter or Space to open it. Choose `事務所` in the bottom navigation or press Escape to return to the Office and restore focus to the launcher. Navigation preserves the current Chat session and does not accept a task suggestion or mark mail as read.

## Personal portraits

Open `%LOCALAPPDATA%\Chihaya\portraits` in Explorer. The app reads the same per-user files from source, Explorer, a shortcut, and a relocated portable folder:

```text
neutral.png
smile.png
focused.png
```

These local exports come from `如月千早\如月千早立ち絵ver2.psd` in `ミリマス式765ver2.1.zip`. The blue jacket uses the `ベース1` layer. Keep that layer and the front hair unchanged across expressions; export transparent PNGs from the upper-left origin of the full 640 × 855 canvas. Do not trim or crop individual expressions. A missing, unreadable, or wrong-sized expression falls back to `neutral.png`, then to the built-in original placeholder. The archive and personal artwork are not bundled or tracked.

## Offline acceptance

| Input | Expected reply | Portrait |
| --- | --- | --- |
| 早上好 | 早上好。今天想先从哪件事开始？ | Smile |
| 我今天要做什么 | 请先列出你的待办和截止时间。我们再决定先做哪一件。 | Focused |

Try the first prompt with Enter and the second with Send. Blank messages should do nothing. Drag and resize the window to check that the portrait stays aligned. Close the app, move the entire portable folder, relaunch it, and repeat both prompts.

To run the automated Qt checks:

```powershell
$env:QT_QPA_PLATFORM = 'windows'
.\.venv\Scripts\python.exe -m unittest discover -v
Remove-Item Env:QT_QPA_PLATFORM
```

### Verification record

- **Passed on Windows 11 x64:** Native Qt tests accepted both inputs, ignored blank input, rendered the placeholder expressions, and checked fallback and alignment at two sizes.
- **Passed with personal artwork:** The source app loaded and rendered all three local PNGs. Pixel changes between the three exports were confined to the face.
- **Passed for the portable build:** Windows UI Automation confirmed both exact replies and their portrait states before and after moving the entire folder. The app closed cleanly and the original output folder was restored.
- **Pending manual sign-off:** Double-click in File Explorer and visually check the two expressions, window dragging, and resizing.

## Claude mode

1. Create `%LOCALAPPDATA%\Chihaya\.env` using [.env.example](.env.example) as a template. Set `ANTHROPIC_API_KEY` in that local file only. Don't paste the key into the chat or put the file in the project or portable folder.
2. Select **Claude** in **Reply mode**. Claude receives the current prompt and up to six recent model-safe turns from the session. Ordinary offline replies stay out of model history; calendar turns can contribute safe scheduling facts. Failed Claude-only requests leave the message in the input for manual retry.
3. The default model is `claude-haiku-5-5`. To use another model, set `ANTHROPIC_MODEL` in the local `.env` or in the app's launch environment; the environment takes precedence.
4. To edit the persona without rebuilding, copy [persona.md](persona.md) to `%LOCALAPPDATA%\Chihaya\persona.md` and make your changes there. The source or bundled persona is used when there is no local override.

For live acceptance, check your Anthropic Console credits, double-click the rebuilt EXE, select **Claude**, and send `I have two tasks tomorrow. What do you need to know to help me prioritize them?` A successful request shows **Claude replied.** and adds a reply to the chat.

### Phase 1C verification

- **Offline:** Both reply and portrait flows passed in the rebuilt Windows executable via UI Automation.
- **Claude with fake responses:** Native Qt and SDK tests passed for the persona, bounded history, expression handling, missing configuration, timeouts, connection and API errors, and manual retry without duplicate requests.
- **Packaging:** The rebuilt executable launched and closed cleanly. Both mode choices were visible, the default persona was bundled, and no `.env` was included.
- **Live Claude reply:** Pending the manual check above. No live request was sent during automated validation.

## Local tasks and Today planning

- **Tasks** works in Offline mode and is a Google-Tasks-style board: a sidebar of categories (checkbox = show that column, `⋯` = rename or delete, `＋ 新建类别`), one column per category with an inline `＋ 添加待办` row and a collapsible `已完成 (n)` section, and a `未完成 / 已完成 / 全部` switch plus title search. `＋ 新建待办` and clicking a card open the editor (title, notes, category, estimate, planned day, time slot, deadline; complete, reopen and delete live there). A blank estimate means **unestimated**. A date-only deadline is a local calendar date; add `HH:MM` to keep a specific time. A planned day is separate from the deadline. Categories exist independently of their tasks and can also be created, renamed and assigned by asking Chihaya (`未分类` is reserved and means no category).
- **Today** lists open tasks that are due, overdue, or planned for today. Enter your available minutes and choose **Suggest order**. The suggestion also considers open tasks with no planned day and separates tasks that fit, tasks that don't fit, and tasks needing an estimate. It never changes dates.
- The source and portable app share `%LOCALAPPDATA%\Chihaya\tasks.sqlite3`. It is created on first run and isn't bundled. Manual **Suggest order** does not use the calendar.
- In **Claude** mode the model, not a phrase list, decides what you mean, in any wording or language. When you simply tell Chihaya your plans ("我今天还要写简历、投三个岗位、跑步"), she first asks whether to put them on the to-do list and shows the exact items; any later "好" or "yes" saves exactly that list. When you ask for a change ("你帮我把这三件事放入待办吧", "move the report to Friday", "把写简历放进 Project ABC 这个类别", "我想新开始一个 project abc，把它作为 category"), the app validates every operation locally, applies it, and appends its own receipt; if anything fails, the receipt lists what changed and what did not, and the model's claim of success is dropped. Supported operations are add (a list becomes one task each), complete/reopen, reschedule, edit title/estimate/notes, create/rename/delete a category, set a task's category, list, plan and undo. Deleting a category always asks first, and its tasks stay uncategorized. Say "撤销刚才的修改" to undo the last change.
- **What the model sees for this:** your messages, the open tasks' ID, title, category, deadline, planned day, time slot and estimate (up to 40), the category names, the last few completed titles, and any proposal waiting for your answer. Task notes never leave the device, and tasks created from an email are shown to the model only as "(task from an email)". Calendar event details and mail content are not part of it. Offline mode sends nothing.

### Phase 2 acceptance

- Offline test: with `筛选岗位` due Friday at 45 minutes and `投递两个岗位` due later at 30 minutes, the 60-minute plan fits the first task and leaves the second unscheduled. Planning preserves deadlines. Completing one task, moving the other's planned day to tomorrow, and reopening the app preserves both changes. A zero-minute estimate is rejected.
- Fake Claude test: the add request saves a task, the one-hour request uses the local planner, ambiguous targets and changes ask for clarification, and a forced SQLite write error leaves existing data intact without reporting success.
- Portable test: UI Automation confirmed the rebuilt EXE could reopen the same isolated database written by the source app, hide the completed task, preserve the other task's planned day and deadline time, and run Today planning.
- Live Claude acceptance was not run. When ready, send `帮我加一项：这周五前投两个岗位，预计 30 分钟。` in Claude mode in the portable EXE and check the task appears. Then send `我今天只有一小时，先做哪项？` and compare the reply with **Today** set to 60 minutes. No live credentials were used during validation.

## Google Calendar (read-only)

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a project you own. Under **APIs & Services → Library**, enable **Google Calendar API**.
2. In **Google Auth Platform → Branding**, configure the app name and support/contact emails. Under **Audience**, choose the appropriate audience. For an **External** app in **Testing**, add the Google account you will sign in with under **Test users**. Google Calendar refresh tokens can expire after seven days in External Testing; use **Reconnect** when prompted. See [Google OAuth token expiration](https://developers.google.com/identity/protocols/oauth2#expiration).
3. Under **Data Access**, add `https://www.googleapis.com/auth/calendar.events.readonly` and `https://www.googleapis.com/auth/calendar.calendarlist.readonly`. Under **Clients → Create client**, select **Desktop app**. Download your client JSON and save it as `%LOCALAPPDATA%\Chihaya\google_calendar_client.json`. Do not paste or upload it elsewhere.
4. Open **Today → Connect Google Calendar**. Complete consent in your browser and return to the app. The desktop flow uses a temporary loopback port. The primary calendar is selected by default. Check additional subscribed calendars, pick a date, and use **Refresh**. The token and selection are stored separately as `google_calendar_token.json` and `google_calendar_selection.json` in the same local folder. They are neither bundled nor tracked.

**Plan with openings** uses the selected calendar date and local start/end window. Use `00:00` as the end to mean midnight at the end of the selected day. Available minutes is an optional cap for calendar plans; **Suggest order** still uses the manual minute budget if the calendar is disconnected or incomplete. Proposed times never change a deadline or planned day.

In chat, try `我今天下午還有哪些空檔？` or `下午兩點到四點，幫我排一下投履歷和複習`. Python calculates all openings and placements. Claude mode can explain that result using only the time blocks and minimal task references; event details and task titles from the database are not sent to Claude as calendar context. The full result is shown locally in both modes. (The separate event-clearing flow below is the one case where event titles for the requested days are shared with Claude.)

Chat resolves dates such as `明天`, `11/23` (current local year), and `2026-11-23` independently of the date picker in **Today**. It uses the same selected calendars and planning window unless the message specifies a different window. A complete matching snapshot can be reused for five minutes; otherwise Chat fetches that date in the background. A calendar with no events, a fully booked window, and a failed refresh have distinct results.

### Local changes on top of the read-only calendar

The app never gets permission to edit Google Calendar, so a schedule change (Thanksgiving, an absent teacher, a study block replaced by a coffee chat) is kept on this computer as an overlay:

- **Clear an event locally (a mask).** Select it on **Today** and choose `在本机取消这个行程`, or ask Chihaya (`今天我没有课，帮我把课程表清理一下`, `今天下午ds301的课老师请假了，那段时间我有其他安排`, `明天早上的grind要因为coffeechat修改，你先帮我把这块schedule清理掉`). The event stays on the page, faded with a dashed outline and a struck-through title, and its time counts as free for openings, the Office cards, the briefing and planning. `恢复这个行程` or `Settings → 恢复全部本机取消的行程` brings it back, and so does asking Chihaya or saying `撤销刚才的修改`.
- **It survives refreshing and reconnecting.** Masks live in `tasks.sqlite3` (table `event_masks`) and are applied to every snapshot the reader returns, cached or freshly read from Google, so a refresh or a new login can never bring a cleared event back. A mask is keyed by calendar, Google's event ID (when present) and the exact start/end, so if Google moves or replaces the event it is treated as a new event and shows up again. Masks for events that ended more than 30 days ago are dropped.
- **The new plan.** A task with a planned day and a saved start/end (create it in `待办`, or tell Chihaya `明天 9 点到 10 点我要 coffee chat`) is drawn on the calendar next to the real events. If it overlaps an event it becomes a narrow outlined lane on the right of the column and the event underneath is drawn lighter; otherwise it is a normal full-width block. Saved slots also count as busy time for openings, and a task that already has a slot is not suggested again by the planner. Nothing is written to Google Calendar.
- **How Chihaya finds the events (Claude mode only).** She cannot see your calendar by default. When a request is about specific events she first asks the app for those dates; the app then gives her, for those days only, each event's id, title, time, calendar name and whether a place is set, and the app validates the ids she picks. Descriptions, attendees, meeting links and addresses are never read or sent. When she had to guess which events you meant, she shows the list and waits for your yes; otherwise the app clears them and lists what it did. Offline mode sends nothing; use the buttons instead.
- **Routing note.** Messages that merely mention `安排` or `schedule` as a noun (`我有其他安排`) now go to Chihaya; the availability shortcut only triggers for requests such as `排一下`, `帮我安排`, `plan my day` or `schedule my afternoon`.

### Phase 3 acceptance

- Mock tests cover two-page reads, expanded recurrence, cancelled and transparent events, busy all-day events, daylight-saving offsets, incomplete refresh, and the manual fallback. In the 14:00–16:00 fixture, the openings were 14:00–14:30 and 15:30–16:00. The 30-minute task fit; the 45-minute task did not. Neither task changed in SQLite.
- All 12 tests passed on Windows. The Phase 3 EXE opened through Explorer's **Open** action, offered sign-in retry when the client file was missing, and still ran the manual planner. The bundle contained no local database, `.env`, or calendar configuration files.
- Live Google authorization was not run. After connecting with your own Desktop client, compare one day's events, one opening, and one plan against the same calendars in Google Calendar. Reopen the EXE and refresh to confirm the selection and authorization are reused.

## Phase 4: Follow-ups, Today briefing, and reminders

- Ask `11/23 15:15 到 16:45，我什麼時候有空？`, then `我其他時間都在做什麼？` or `那下午四點呢？`. Follow-ups reuse the original date, window, and calendars even if you change the Today date picker. Event names and times are rendered locally; Claude gets only the computed scheduling facts, not event names or private event details. **Clear conversation** resets the history, calendar context, and any pending confirmation.
- If Claude asks you to confirm a concrete task change, `yes please` or `好` applies it once. `no thanks` or `取消` discards it. The app acknowledges the change only after SQLite commits.
- **Show Today briefing** displays the current local date, next event, due or overdue tasks, next opening, and the next task the planner can fit. It works without Claude. A failed calendar read leaves the task briefing available.
- Enable **Task reminders** in Today. Timed deadlines trigger at their local deadline; date-only deadlines and planned days trigger at 09:00 local time. Alerts can catch up later that day while the app is running, but do not replay previous days. **Reminder history** records alerts even if Windows suppresses notification popups. Planner suggestions are not saved schedules and do not trigger alerts.
- **Close to tray** keeps the app and its reminders running when you close the window. The tray menu offers **Open**, **Today's briefing**, and **Quit**. Quit stops reminders. Preferences persist in `%LOCALAPPDATA%\Chihaya\settings.ini`.

### Phase 4 acceptance

- The Windows test suite passed 19 tests. The fixed New York fixture found 15:15–15:30 free and 15:30–16:45 busy. Both overlapping events appeared locally after a date-picker change and history trim; neither title appeared in the captured Claude requests. The suite also checks one-time confirmation, rejection, a failed write, reminder deduplication after reopening and rescheduling, and close/reopen through the tray.
- Windows Shell **Open** launched both the original build and a copy extracted from the ZIP to another directory. UI Automation found the Chat controls, and both runs used the same isolated profile and exited cleanly.
- For live acceptance, open the packaged app, refresh your connected calendar, ask about a date and time with known events, and then ask `我其他時間都在做什麼？`. In Claude mode, ask for a proposed 30-minute task and confirm it once with `yes please`; check **Tasks** to verify a single new row. Live Google and Claude requests were not part of the automated test run.

## Gmail v1 (optional, read-only)

1. In the same Google Cloud project used for Calendar, [enable the Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com). Add `https://www.googleapis.com/auth/gmail.readonly` in **Google Auth Platform → Data Access**. If the app is **External** and in **Testing**, add your Google account as a test user. Testing refresh tokens can expire after seven days; use **Reconnect Gmail** when prompted. Gmail read-only is a [restricted scope](https://developers.google.com/workspace/gmail/api/auth/scopes); organization policies and Google verification requirements may limit access.
2. Keep the existing Desktop OAuth client at `%LOCALAPPDATA%\Chihaya\google_calendar_client.json`. In the app, open **Today → Mail → Connect Gmail** and finish consent in your browser. Gmail saves its own `%LOCALAPPDATA%\Chihaya\gmail_token.json` and does not replace the Calendar token.
3. Use **Refresh mail** to show up to ten unread inbox messages for **Today**, **Today: job matches**, or **All dates**. Today uses the app's local time zone. Edit the comma-separated job phrases to change what Gmail matches. Select a row to read a plain-text preview without marking it read or loading remote images.
4. In Chat, try `今天有什麼未讀郵件？` or `今天有什麼求職相關郵件？`. They use the same Gmail search as Today and work in Offline mode. The app does not send Gmail results or previews to Claude, including in later conversation history. Avoid manually pasting private email into Claude-mode Chat. Gmail v1 does not summarize email with an external model.
5. **Disconnect Gmail** removes the local token and clears displayed results and previews. It does not revoke Google's grant server-side. You can manage grants in [Google Account connections](https://myaccount.google.com/connections); revoking the shared app there may also affect Calendar.

Gmail reconnect, disconnect, and mail refresh are unavailable while a Gmail request or Daily Brief scan is running. Wait for the current operation to finish before changing accounts. A successful reconnect clears the previous account's mail preview, cached Daily Brief results, and locally displayed Gmail replies in Chat.

**Live acceptance:** In the new EXE, connect Gmail and refresh. Compare a message with Gmail's `in:inbox is:unread` search, using the same date filter (or **All dates**). Try the unread-mail Chat prompt and compare it with the Today mail filter. Close the app fully, reopen it, and verify that both Gmail and Calendar can refresh with their saved authorizations.

**Verification:** 25 Windows tests passed, including local-day filtering across New York's daylight-saving transition, the ten-message limit, preview without a write, distinct empty and failed refresh states, and no mail data in a later mocked Claude request. The original and relocated portable builds passed Windows UI Automation for the Mail controls and disconnected Chat response. Live Gmail and Calendar authorization were not tested.

## Daily Brief

- Open **Today → Refresh Daily Brief** to combine open tasks, calendar openings, and **Mail to follow up**. The app considers up to 100 unique inbox threads from seven local days and checks up to 30 for meaning, including read mail. It reserves capacity for a broad inbox search and for higher-signal results. Scan details distinguish search exhaustion, budget limits, partial results, and stale data. **Review** flags a plausible obligation whose next action or date is unclear. The scan is not a complete inbox audit.
- The built-in on-device classifier is enabled by default and needs no download, API key, or per-request service. To change defaults, close the app and edit the `[gmail]` section of `%LOCALAPPDATA%\Chihaya\settings.ini`. Set `candidate_threads=100` (range 2–200), `content_checks=30` (1 through the candidate limit), or `local_classifier=false` for the conservative rules-only fallback. Set `local_classifier=true` to re-enable on-device classification.
- Select a suggestion to **Show excerpt**, **Open source message**, or **Add to Tasks**. Adding twice won't create another task. The planner schedules saved local tasks only; Gmail suggestions are not automatically scheduled. An unclear deadline is shown as written, not saved as a guessed date.
- Try `give me a daily brief`, `Give me my daily brief`, `daily brief`, `show me today's briefing`, `今天有什麼要處理？`, or `今天有什么要处理？`. For mail alone, try `Any emails I need to follow up on?`, `有哪些信要回？`, or `有哪些邮件要回？`. The brief runs locally in both reply modes. Gmail results and email-linked task details are excluded from Claude inputs. `I have one hour, what should I do?` still runs the manual planner.
- To verify with your own account, connect Gmail using the setup above, refresh the brief, and compare a suggested action against its original message. Check the recommendation against **Tasks**, then ask for the brief in another language. Fully quit and reopen the EXE to verify saved Gmail and Calendar authorization.
- **Verified on Windows:** All 29 tests passed. Synthetic mail covered the older read interview, job-match digest versus a real invite, thread replies, both scan budgets, partial results, and the rules-only fallback. Both English phrases passed in the original and relocated EXE with Gmail disconnected. Multipart preview passed in the Qt fixture; live preview and saved Google authorization remain signed-in checks.
