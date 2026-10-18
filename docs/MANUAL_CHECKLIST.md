# Manual browser checklist

`make e2e` covers the HTTP API end to end. These checks need a person and a browser, because they are about keys, focus, dialogs, media playback and layout. Run them on a stack started with `make up`, after every phase that touches the UI and before a release. Mark each line PASS or FAIL.

## Preparation

1. `make up`. Set `APP_PASSWORD` in `.env` for the login checks (then `make down && make up`).
2. Create test media: upload at least 25 images and 2 videos at /upload (any photos you own; the demo add-on on /sources can create images: "Demo images", count 25, Run now). For compare mode you need an image and a copy of it resized to about 50 percent. For video seeking, a video of at least 30 seconds (MP4 H.264 or WebM).
3. In the library, create folders "Animals" and "Animals / Cats" (Library, "New top-level folder", then "New subfolder of Animals").

Use a desktop browser (Firefox or Chromium) for sections 1 to 7 and its device mode (or a phone) for section 8.

## 1. Keyboard-only review of 20 files, including undo

Open /review. Do not touch the mouse from here on.

| # | Action | Expected |
| --- | --- | --- |
| 1.1 | Press `?` | A dialog lists the review keys (J/K, A, D, S, C, X, Shift+A, Shift+D, U, G, Enter). `Esc` closes it. |
| 1.2 | Press `J` three times, then `K` once | The single view moves forward three files and back one; the side panel (name, size, dimensions, sources, tags) changes with each file. |
| 1.3 | Press `S` | The file is skipped: the next one is shown, "Remaining" does not change. |
| 1.4 | Press `A`, then `Enter` | The archive panel opens with the last used folder (the Inbox on first use) selected; Enter archives the file. A toast confirms it, "Remaining" drops by 1, "Decided this session" rises by 1. |
| 1.5 | Press `D` | The file is discarded at once, with a toast and an Undo button. |
| 1.6 | Repeat `A Enter` and `D` until 20 files are decided | Each key decides exactly one file; nothing is decided twice; the next file appears without a reload. |
| 1.7 | Press `U` | The last decision is undone: that file is shown again and "Remaining" grows by 1. |
| 1.8 | Press `U` again | The decision before it is undone too. |
| 1.9 | Press `G`, then `X` on two tiles (move with `J`/`K`), then `Shift+D` | Grid view; the two tiles are marked selected; Shift+D discards both; one `U` brings both back. |
| 1.10 | Click into the archive panel's tag field (with `A`), type `dada`, press `Esc` | Typing in a field never triggers shortcuts (no archive or discard happened); Esc closes the panel without archiving. |
| 1.11 | Check in Library and on the dashboard | The archived files are in their folders; the dashboard counts match the decisions minus the undone ones. |

## 2. Compare mode

| # | Action | Expected |
| --- | --- | --- |
| 2.1 | Upload an image, then its 50 percent copy. Open /review and go to the copy | The side panel's similar strip shows the original with its status and distance. |
| 2.2 | Press `C` | Compare view: both files side by side with dimensions, size, format and dates; the larger resolution is highlighted. |
| 2.3 | Press `?` | The compare keys are listed: `1` keep left, `2` keep right, `B` keep both, `R` replace, `C` next similar file, `Esc` back. |
| 2.4 | Press `1` | The left file is archived, the right one discarded; the view moves on. `U` undoes it. |
| 2.5 | Press `C` again, then `B` | Both files are kept (archived); the pair no longer appears as similar. |
| 2.6 | Archive an original, upload a resized copy, open it and press `C`, then `R` | Replace: the new file is archived into the old file's folder with its tags, the old one discarded. `U` undoes both. |
| 2.7 | Press `C` then `Esc` | Back to the single view without a decision. |

## 3. Archive panel: folder picker and tag chips

| # | Action | Expected |
| --- | --- | --- |
| 3.1 | On a file from the demo source, press `A` | The panel preselects the source's default folder (if set); the crawler tags (e.g. `demo`, `artist:test`) appear as chips. Recent folders are listed as buttons. |
| 3.2 | Type `cat` in the folder search | The tree filters to matching folders and the first match ("Animals / Cats") is selected; arrow keys move through the matches. |
| 3.3 | Click × on the `demo` chip | The chip disappears. |
| 3.4 | Type `favourite` in the tag field and press `Enter` | A user tag chip `favourite` is added; the panel stays open. |
| 3.5 | Type a name in "New subfolder of the selected folder" and press `Enter` | The subfolder is created under the selected folder and selected. |
| 3.6 | Press `Enter` (focus outside the text fields) | The file is archived. Its page (/files/<id>) shows the chosen folder, the tags `favourite` and `artist:test`, and `demo` under "Removed tags". |
| 3.7 | Tick "Keep these user tags for the next files", archive another file | The next panel starts with the same user tags. |

## 4. Library: Move to folder, and folder deletion

| # | Action | Expected |
| --- | --- | --- |
| 4.1 | Open /library, choose "Inbox" in the tree | The grid shows the Inbox's files; the tree shows counts as "direct / with subfolders". |
| 4.2 | Click "Select page" (or `X` on several tiles), choose "Animals / Cats" in the selection bar's folder list, press Move (or `M`) | The files leave the grid; after the reload the tree counts of Inbox and Animals / Cats change by the number moved. |
| 4.3 | Add a tag with the selection bar (`T`), then remove it | The tiles show, then no longer show, the tag. |
| 4.4 | Select "Animals", tick "Move files and subfolders to the top level", click "Delete folder" and confirm | Animals is gone; its files are in the Inbox, "Cats" is a top-level folder. No archived file is left without a folder. |
| 4.5 | Try to delete or rename the Inbox | Refused with a message. |
| 4.6 | Delete a non-empty folder without the move option | Refused (409) with a message; nothing changes. |

## 5. Search box autocomplete and panel sync

| # | Action | Expected |
| --- | --- | --- |
| 5.1 | On any page, type `cat` in the navigation search box and press Enter | /search opens with `cat` in the query box and results. |
| 5.2 | In the query box, type `-d` | Tag suggestions starting with d appear; arrow keys and Enter or Tab insert one as `-<tag>`. |
| 5.3 | Type `folder:` | Folder paths are suggested (quoted, e.g. `folder:"Animals/Cats"`). Also `type:`, `status:` and `source:` suggest their values. |
| 5.4 | Type an invalid token such as `collected:2026-13-01` | An error naming the character position appears under the query box. |
| 5.5 | Type `cat -dog folder:"Animals" type:image` and wait a moment | The filter panel (`F` toggles it) shows cat under must have, dog under must not have, Animals as folder, image ticked. |
| 5.6 | Tick "video" in the panel | The query box and the address bar gain `type:video`; the results reload. |
| 5.7 | Save the search in the sidebar, open another page, open the saved search | Same query, same results. Rename and delete work. |
| 5.8 | Press `/` | Focus jumps to the query box. |

## 6. Video seeking

| # | Action | Expected |
| --- | --- | --- |
| 6.1 | Open /files/<id> of a video of 30 s or more | The video plays with controls. |
| 6.2 | Drag the position slider to about 75 percent | Playback continues from there within a second or two (the media route answers Range requests with 206). |
| 6.3 | Seek back to the start, then forward again | Works repeatedly, without downloading the whole file first. |
| 6.4 | In /review, the same video in the single view | Plays and seeks the same way; arrow keys go to the focused player, not to the review navigation. |

## 7. Login and logout

With `APP_PASSWORD` set:

| # | Action | Expected |
| --- | --- | --- |
| 7.1 | Open /library in a private window | Redirect to /login. |
| 7.2 | Enter a wrong password twice | "Wrong password." each time; the second answer comes about a second later (slowdown). |
| 7.3 | Enter the right password | Redirect back to /library. No "No password set" banner. |
| 7.4 | Close and reopen the browser (not private) | Still signed in (the cookie lasts 30 days). |
| 7.5 | Click "Log out" | Back to /login; /library redirects to /login again. |
| 7.6 | Without `APP_PASSWORD` | Every page shows the "No password set" banner and there is no Log out button. |

## 8. Phone width (layouts must not break)

In the browser's device mode at 390 × 844 (or on a phone), visit each page: /, /upload, /review (single, grid and compare views, archive panel), /library, /files/<id> (image and video), /search (with the filter panel open), /tags, /sources, /sources/new, /jobs, /jobs/<id>, /settings, /login.

| # | Check | Expected |
| --- | --- | --- |
| 8.1 | Horizontal scrolling of the page | None. Wide tables (/jobs, /sources, /tags, maintenance jobs on /settings) scroll sideways inside their own box. |
| 8.2 | Navigation | Wraps onto several lines; every link, the search box, `?` and Log out are reachable. |
| 8.3 | Text and controls | Nothing overlaps or is cut off; buttons and inputs fit the screen width; long file names and tags wrap. |
| 8.4 | Dialogs (`?` help, archive panel) | Fit on the screen and scroll inside when taller than it. |
| 8.5 | Media | Images and videos scale to the screen width. |
| 8.6 | Upload by tapping "choose files" | Works on a phone (drag and drop is desktop only). |
