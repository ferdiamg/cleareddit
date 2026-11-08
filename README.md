<div align="center">
  <img src="assets/cleareddit-logo.png" alt="cleareddit logo" width="320">
  <p><strong>Delete your Reddit history from the terminal.</strong></p>
  <p>A free, local Redact alternative for your own Reddit comments and posts.</p>
</div>

## What it does

- Overwrite comments and text posts with random words, then delete them. You can also delete without an edit.
- Scan your account and import a Reddit data export to find older items.
- Process multiple accounts. Keep session cookies and progress on your computer.
- Pause for rate limits and resume after an interruption.

cleareddit works with posts and comments. It does not process chats, messages, or other platforms. It uses direct HTTP requests, not browser automation.

## How the connection works

The script runs on your computer. The session cookie lets Reddit recognize your signed-in account, as it does in your browser. When you add an account, cleareddit sends a request directly to Reddit to confirm the account name. It saves the cookie in the local `.cleareddit/` folder. Later, Python sends HTTPS requests directly to Reddit to list, edit, and delete your items. There is no cleareddit server or telemetry.

## Get started

1. Sign in to Reddit in your browser.
2. Open **DevTools → Application → Cookies → `reddit.com`**. Find `reddit_session` and copy its full **Value**. It starts with `ey...`.
3. Add the account from the project directory:

   ```bash
   python3 cleareddit.py session-add --account YOUR_USERNAME
   ```

   Paste the cookie when the terminal asks for it. The input is hidden. Treat this cookie like a password. Do not put it in a command or share it.

Repeat these steps for each account. The tool checks that each cookie belongs to the account you specify.

Scan your accounts and review the plan:

```bash
python3 cleareddit.py scan
python3 cleareddit.py plan
python3 cleareddit.py run
```

`run` without `--yes` only shows counts. To test five comments and then process all scanned items, run:

```bash
python3 cleareddit.py run --account YOUR_USERNAME --kinds comments --limit 5 --yes
python3 cleareddit.py run --yes
```

By default, the tool tries to replace each comment and text post with four or five random words before deletion. The replacement has at least 30 characters. It deletes link and media posts directly. Use `--mode delete` to skip replacement. Use `--replacement 'Your text here'` to set your own text.

## Find older items

Reddit account lists can stop at about 1,000 items per type. A [Reddit data export](https://www.reddit.com/settings/data-request) can supply older IDs. [Reddit says](https://support.reddithelp.com/hc/en-us/articles/360043048352-How-do-I-request-a-copy-of-my-Reddit-data-and-information) that the export can take up to 30 days. You can scan now and import the export later.

```bash
python3 cleareddit.py import --account YOUR_USERNAME /path/to/reddit-export.zip
python3 cleareddit.py plan
python3 cleareddit.py run --account YOUR_USERNAME --yes
```

Import each export under its own account name. The tool reads `comments.csv` and `posts.csv`. It stores IDs and available links, but not comment text or post titles. Before each change, it checks that the item belongs to the signed-in account.

## Data and limits

- `.cleareddit/` holds session cookies and progress. It has restricted file permissions and is in `.gitignore`. Do not publish this directory.
- `run` shows an excerpt of each item before it changes it. `--full-content` shows the complete text. Redirected terminal output can save this text to a file.
- The tool saves each result. Restart `run` after an interruption. It retries failed items and waits when Reddit sets a rate limit. An item marked `missing` is not a confirmed deletion.
- Deletion cannot remove copies made elsewhere. [Deleting a Reddit account does not delete its posts and comments](https://support.reddithelp.com/hc/en-us/articles/360043047932-If-I-delete-my-account-what-happens-to-my-username-posts-and-comments).
- The web-session endpoints can change or stop working. [Reddit requires approval for API access](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy). An approved OAuth app can use the optional `init` and `auth` commands.

## Test

```bash
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE). cleareddit is an independent project. It is not affiliated with Reddit or Redact.
