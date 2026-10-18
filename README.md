# Quip → Google Docs

Exports your Quip documents and spreadsheets into a folder tree that you drag into Google Drive. No Google Cloud project or Google credentials needed.

## Secrets

- The Quip token is stored only in the macOS Keychain. It never goes into files, command arguments or output.
- Script output shows only IDs and counts. Run it in your own terminal.
- `staging/` is readable only by you. Keep FileVault on.

## Setup

Run all commands from this folder.

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python secrets_setup.py set
```

The last command explains how to get a token from `https://quip.com/dev/token`, then asks for it at a hidden prompt.

If you move this folder, delete `.venv` and redo the first two commands.

## Migrate

1. Run a trial export:

   ```bash
   .venv/bin/python quip_export.py --limit 5
   ```

2. In Google Drive: **Settings → General → Convert uploads**: on.
3. Open the export folder:

   ```bash
   open staging/upload
   ```

4. Drag the `Quip import YYYY-MM-DD` folder into Drive in your browser.
5. If the trial looks right, delete it in Drive, run the export without `--limit`, and upload again. With many documents, upload a few subfolders at a time.

Re-running the export only downloads new or changed documents.

**Options:**
- `--folder ID`: export one Quip folder only.
- `--api-base URL`: for company sites on their own host.
- `--no-recent`: skip documents that aren't in any folder.

## Not migrated

- Comments, edit history, slides and chat rooms.
- A document in several Quip folders only appears in the first one.
- Links between documents still point to Quip.
- `.html` files are documents Quip couldn't export as .docx. In Drive, use **Open with → Google Docs**.

## Cleanup

1. Remove the token from Keychain:

   ```bash
   .venv/bin/python secrets_setup.py clear
   ```

2. Revoke the token on Quip's `/dev/token` page.
3. Move `staging/` to the Trash.
