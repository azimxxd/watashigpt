# Advanced configuration

Normal writing, language selection, saved actions and AI connection setup are available in the UI.
The following features are for existing users and custom deployments.

## Providers

Set `llm.provider`, `llm.model`, `llm.base_url` and `llm.request_options` in
`action-middleware/config.yaml`. Existing files are not rewritten on upgrade; new core writing
actions are merged in memory. API keys resolve from `ACTIONFLOW_API_KEY`, then a configured key,
then the system keyring. A key set through the GUI is used immediately and saved to the keyring;
an environment override still takes precedence on a subsequent launch.

`llm.fallback` has its own provider, model, endpoint, timeout and request options. It does not
inherit the primary endpoint or command-specific model override. With a local primary and a
remote backup, selected text can leave the machine when fallback is used.

```sh
python main.py --check
python main.py --set-key groq
```

The setup window verifies credentials before persisting them. Cancelling a connection attempt
prevents the pending result from being saved. A working system keyring is required for GUI key
persistence. On Linux services, use a protected environment file when the desktop keyring is
unavailable.

## Legacy commands

Examples: `FIX: text`, `CLEAR: text`, `SHORT: text`, `TRANS:RU: text`, `SUM: text`,
`TONE:friendly: text`, `FMT: {"a":1}`, `B64: text`. Prefix commands run immediately, without the
writing palette's preview. Chains such as `POL:|SUM: text` also remain available.

`CMD:`/`RUN:`/`EXEC:`, `IMG:`/`IMAGE:`, `WIKI:` and `DEFINE:` no longer run. Old config entries
cannot re-enable these actions through dispatch. Their implementation is retained internally
for compatibility tests, not exposed as a product feature.

Legacy personal commands in `config.yaml` still appear alongside saved actions. For new reusable
instructions, prefer Save action in the preview. It needs no YAML and stores no example selection.

`history.log_text: true` opts into text in history/diagnostic logs. Password, redaction, command
and repeat output remain masked. Previously written logs are not rewritten on upgrade.

## Start at login

macOS:

```sh
python main.py --install
python main.py --uninstall
```

Grant Accessibility and Input Monitoring to the Python path printed by the installer.
Logs: `~/Library/Logs/ActionFlow.log`.

Linux:

```sh
sudo -E python main.py --install
```

Service keys can be provided in `/etc/actionflow.env` (mode 600) as `ACTIONFLOW_API_KEY=...`.
Inspect service output with `journalctl -u actionflow -f`.

## Manual smoke checks

1. Open the practice window; connect a provider, or cancel setup. No text should be injected elsewhere.
2. In a text editor, select a sentence with a typo. Run Fix mistakes, inspect Original/Changes/Result,
   then replace. Select the resulting sentence and undo.
3. Start an edit, change the source selection, then accept. The result must remain available to copy.
4. Copy another item immediately after replacing; the delayed restore must not overwrite the new copy.
5. Run a custom instruction, save it, restart, and apply it to different text. Remove it in Settings.
6. Change the translation language and restart. Translation should use the chosen language directly.
7. Disconnect the network during generation. Partial output must not be offered for replacement.
8. On Linux, check both a normal editor and a terminal; when focus cannot be verified, copy manually.
