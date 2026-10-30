"""Posts World of Warcraft: Forever patch notes and hotfixes to Discord as a bot.

Run it on a schedule. Each run compares Blizzard's posts to state.json and
sends only what is new. Blizzard adds to and retitles hotfix posts under the
same id, and sometimes bumps "updated" on old posts in bulk, so changes are
detected by comparing the text line by line rather than by id or timestamp.
"""

import argparse
import dataclasses
import datetime
import hashlib
import html.parser
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

SITE = "https://worldofwarcraft.blizzard.com"
SOURCES = [f"{SITE}/en-us/content-update-notes", f"{SITE}/en-us/news"]
USER_AGENT = "Mozilla/5.0 (compatible; wow-patch-bot/1.0)"
DISCORD_API = "https://discord.com/api/v10"
DISCORD_USER_AGENT = "DiscordBot (https://github.com, 1.0)"
STATE_FILE = pathlib.Path(__file__).with_name("state.json")

NOTES_TAGS = {"update notes", "patch notes", "hotfixes", "content notes"}
NOTES_TITLE = re.compile(r"\b(hotfix(es)?|patch notes|update notes)\b", re.I)
DATE_LINE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October"
    r"|November|December) \d{1,2}(, \d{4})?$"
)
FOREVER_WORD = re.compile(r"\bforever\b", re.I)
FOREVER_NAME = re.compile(r"\b(wow|world of warcraft):?\s+forever\b", re.I)
FOREVER_LABEL_LENGTH = 40  # A short line with the word is a section label, not a sentence.

DESCRIPTION_LIMIT = 3900  # Discord allows 4096; leaves room for the footer line.
MAX_POSTS_PER_RUN = 5  # Above this it's a Blizzard bulk edit, not real news.
MAX_UPDATE_AGE_DAYS = 60  # Edits to posts older than this are recorded silently.
MAX_TRACKED_POSTS = 60
MAX_DOWN_DAYS = 2  # Fail the run (so GitHub emails you) after this long unreachable.

COLOR = 0x3FC7EB


class ParseError(Exception):
    """Blizzard's page no longer looks the way this script expects."""


@dataclasses.dataclass
class Line:
    depth: int  # 0 date/heading, 1 section, 2+ body and nested bullets
    text: str  # Discord markdown
    plain: str


class _NotesParser(html.parser.HTMLParser):
    """Flattens a post's HTML into Lines, keeping its heading and list nesting."""

    BLOCKS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.list_depth = 0
        self.block = None
        self.parts = []
        self.plain_parts = []
        self.bold = 0
        self.italic = 0
        self.bold_chars = 0
        self.skip_anchor = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS or tag in ("ul", "ol"):
            self._flush()
        if tag in ("ul", "ol"):
            self.list_depth += 1
        elif tag in self.BLOCKS:
            self.block = tag
        elif tag in ("strong", "b"):
            self.bold += 1
        elif tag in ("em", "i"):
            self.italic += 1
        elif tag == "a" and (dict(attrs).get("href") or "").startswith("#"):
            self.skip_anchor += 1  # table of contents and back-to-top links
        elif tag == "br":
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self._flush()
            self.block = None
        elif tag in ("ul", "ol"):
            self._flush()
            self.list_depth = max(0, self.list_depth - 1)
        elif tag in ("strong", "b"):
            self.bold = max(0, self.bold - 1)
        elif tag in ("em", "i"):
            self.italic = max(0, self.italic - 1)
        elif tag == "a" and self.skip_anchor:
            self.skip_anchor -= 1

    def handle_data(self, data):
        if self.skip_anchor:
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip():
            if self.parts:
                self.parts.append(" ")
                self.plain_parts.append(" ")
            return
        self.plain_parts.append(text)
        if self.bold:
            self.bold_chars += len(text.strip())
            text = f"**{text.strip()}**"
        elif self.italic:
            text = f"*{text.strip()}*"
        self.parts.append(text)

    def _flush(self):
        plain = "".join(self.plain_parts).strip()
        text = "".join(self.parts).strip().replace("****", "")
        all_bold = self.bold_chars >= len(plain.replace(" ", ""))
        block, self.parts, self.plain_parts, self.bold_chars = self.block, [], [], 0
        if not plain or plain == "TABLE OF CONTENTS":
            return
        if block and block[0] == "h":
            self.lines.append(Line(0 if block in ("h1", "h2") else 1, f"**{plain}**", plain))
        elif self.list_depth:
            indent = "  " * (self.list_depth - 1)
            self.lines.append(Line(1 + self.list_depth, f"{indent}- {text}", plain))
        elif DATE_LINE.match(plain):
            self.lines.append(Line(0, f"**{plain}**", plain))
        elif all_bold:
            self.lines.append(Line(1, text, plain))
        else:
            self.lines.append(Line(2, text, plain))


def parse_lines(content):
    parser = _NotesParser()
    parser.feed(content)
    parser.close()
    parser._flush()
    return parser.lines


def _parent_indexes(lines):
    """For each line, the index of its nearest shallower line above, or None."""
    parents = []
    stack = []
    for i, line in enumerate(lines):
        while stack and lines[stack[-1]].depth >= line.depth:
            stack.pop()
        parents.append(stack[-1] if stack else None)
        stack.append(i)
    return parents


def line_keys(lines):
    """Stable hashes of each line. They include the path of parent headings, so
    identical wording under a new date or class still counts as new."""
    parents = _parent_indexes(lines)
    keys = []
    for i, line in enumerate(lines):
        path = []
        at = i
        while at is not None:
            path.append(lines[at].plain)
            at = parents[at]
        keys.append(hashlib.sha1("\n".join(path).encode()).hexdigest()[:12])
    return keys


def _names_forever(text):
    if FOREVER_NAME.search(text):
        return True
    return len(text) <= FOREVER_LABEL_LENGTH and bool(FOREVER_WORD.search(text))


def forever_indexes(post, lines):
    """Indexes of the lines about Forever. Blizzard's shared hotfix posts cover
    every game, so those are filtered down to the Forever sections."""
    if FOREVER_WORD.search(post["title"]):
        return set(range(len(lines)))
    parents = _parent_indexes(lines)
    wanted = set()
    for i, line in enumerate(lines):
        if line.depth == 2 and parents[i] is None:
            continue  # A post's intro names every game it covers.
        at = i
        while at is not None:
            if _names_forever(lines[at].plain):
                wanted.add(i)
                break
            at = parents[at]
    return wanted


def new_lines(lines, keys, known, wanted):
    """Wanted lines not in known, plus the headings needed to make sense of them."""
    parents = _parent_indexes(lines)
    keep = set()
    for i, key in enumerate(keys):
        if i in wanted and key not in known:
            at = i
            while at is not None and at not in keep:
                keep.add(at)
                at = parents[at]
    return [lines[i] for i in sorted(keep)]


def extract_posts(page):
    """Pulls the post objects Blizzard embeds as JSON in its listing pages."""
    decoder = json.JSONDecoder()
    posts = {}
    for match in re.finditer(r'\{"title":"', page):
        try:
            post, _ = decoder.raw_decode(page[match.start():])
        except ValueError:
            continue
        if post.get("id") and post.get("slug") and post.get("content"):
            posts[post["id"]] = post
    return posts


def is_notes(post):
    tags = {tag.lower() for tag in post.get("tags") or []}
    return bool(tags & NOTES_TAGS) or bool(NOTES_TITLE.search(post["title"]))


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


def fetch_notes():
    posts = {}
    for url in SOURCES:
        found = extract_posts(fetch(url))
        if not found:
            raise ParseError(f"no posts found in {url}, the page layout may have changed")
        posts.update(found)
    return sorted(
        (post for post in posts.values() if is_notes(post)),
        key=lambda post: post.get("published") or "",
    )


def build_message(post, lines, is_new):
    url = SITE + "/en-us" + post["url"]
    body = ""
    for i, line in enumerate(lines):
        if len(body) + len(line.text) + 1 > DESCRIPTION_LIMIT:
            body += f"\n\n...and {len(lines) - i} more lines in the full notes."
            break
        body += ("\n" if body else "") + line.text
    image = (post.get("thumbnail") or {}).get("url", "")
    embed = {
        "title": post["title"][:256],
        "url": url,
        "description": body,
        "color": COLOR,
        "footer": {"text": "New post" if is_new else "Updated with new changes"},
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if image:
        embed["thumbnail"] = {"url": "https:" + image if image.startswith("//") else image}
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def discord_headers():
    return {
        "Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}",
        "Content-Type": "application/json",
        "User-Agent": DISCORD_USER_AGENT,
    }


def discord_call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        DISCORD_API + path, data=data, headers=discord_headers(), method=method
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def set_name(name):
    """Sets the bot's username and server nickname. The nickname is what the
    server shows, so it has to change too if one was set."""
    me = discord_call("GET", "/users/@me")
    channel = discord_call("GET", f"/channels/{os.environ['DISCORD_CHANNEL_ID']}")
    guild = channel["guild_id"]
    nick = discord_call("GET", f"/guilds/{guild}/members/{me['id']}").get("nick")
    print(f"current username: {me['username']}, server nickname: {nick}")
    changes = []
    if me["username"] != name:
        changes.append(("username", "/users/@me", {"username": name}))
    if nick not in (None, name):
        changes.append(("nickname", f"/guilds/{guild}/members/@me", {"nick": name}))
    failed = False
    for label, path, body in changes:
        try:
            discord_call("PATCH", path, body)
            print(f"{label} changed to {name}")
        except urllib.error.HTTPError as error:
            failed = True
            print(f"could not change {label}: {error.read().decode(errors='replace')}")
    if not changes:
        print("names already match")
    return 1 if failed else 0


def send(message):
    url = f"{DISCORD_API}/channels/{os.environ['DISCORD_CHANNEL_ID']}/messages"
    data = json.dumps(message).encode()
    headers = discord_headers()
    for attempt in range(3):
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            urllib.request.urlopen(request, timeout=30).close()
            return
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == 2:
                print(f"Discord refused the message: {error.read().decode(errors='replace')}")
                raise
            time.sleep(float(error.headers.get("Retry-After") or 5))


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_ok": None, "posts": {}}


def save_state(state):
    tracked = sorted(state["posts"], key=int)
    for post_id in tracked[:-MAX_TRACKED_POSTS]:
        del state["posts"][post_id]
    STATE_FILE.write_text(json.dumps(state, indent=0, sort_keys=True) + "\n", encoding="utf-8")


def too_old_to_announce(post, now):
    published = datetime.datetime.fromisoformat(post["published"].replace("Z", "+00:00"))
    return (now - published).days > MAX_UPDATE_AGE_DAYS


def run(posts, state, post_message, now, persist=True):
    """Announces changes and records them. Saves after every send so a crash
    part way through can't repeat a message or lose one."""
    first_run = not state["posts"]
    sent = 0
    for post in posts:
        post_id = str(post["id"])
        lines = parse_lines(post["content"])
        keys = line_keys(lines)
        known = state["posts"].get(post_id)
        changed = new_lines(lines, keys, set(known or []), forever_indexes(post, lines))
        if known == keys:
            continue
        quiet = first_run or not changed or sent >= MAX_POSTS_PER_RUN
        quiet = quiet or (known is not None and too_old_to_announce(post, now))
        if not quiet:
            post_message(build_message(post, changed, known is None))
            sent += 1
            print(f"sent: {post['title']} ({len(changed)} lines)")
        elif changed and not first_run:
            print(f"recorded without posting: {post['title']}")
        state["posts"][post_id] = keys
        if persist:
            save_state(state)
    if first_run:
        print(f"first run: recorded {len(posts)} existing posts, nothing sent")
    return sent


def test_update(now):
    """Sends a made-up post and an update to it through the real pipeline, then
    checks that a repeat sends nothing. state.json is not touched."""
    def fake(content):
        return {
            "id": 1,
            "title": "TEST, NOT REAL: WoW: Forever Hotfixes",
            "url": "/news",
            "published": now.isoformat(),
            "content": content,
        }

    first = ("<p><strong>Test Hotfixes</strong></p>"
             "<ul><li>Orgrimmar's walls now 10% spikier.</li></ul>")
    second = first + ("<p><strong>Test Update</strong></p>"
                      "<ul><li>Peons now complain 20% less.</li></ul>")
    state = {"last_ok": None, "posts": {"0": []}}
    results = [
        run([fake(first)], state, send, now, persist=False),
        run([fake(second)], state, send, now, persist=False),
        run([fake(second)], state, send, now, persist=False),
    ]
    print(f"messages sent: new post {results[0]}, update {results[1]}, repeat {results[2]}")
    return 0 if results == [1, 1, 0] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent and change nothing")
    parser.add_argument("--test-post", action="store_true",
                        help="send the latest post to Discord now, to check the bot")
    parser.add_argument("--set-name", help="rename the bot in Discord and exit")
    parser.add_argument("--test-update", action="store_true",
                        help="send a labelled fake post and an update to it")
    args = parser.parse_args()

    if args.set_name:
        return set_name(args.set_name)

    state = load_state()
    now = datetime.datetime.now(datetime.timezone.utc)
    if args.test_update:
        return test_update(now)
    try:
        posts = fetch_notes()
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"Blizzard unreachable: {error}")
        if state["last_ok"]:
            last_ok = datetime.date.fromisoformat(state["last_ok"])
            if (now.date() - last_ok).days < MAX_DOWN_DAYS:
                return 0  # Their site blips. Retry next run rather than alert.
        return 1

    if args.dry_run or args.test_post:
        for latest in reversed(posts):
            lines = parse_lines(latest["content"])
            kept = new_lines(lines, line_keys(lines), set(), forever_indexes(latest, lines))
            if kept:
                break
        else:
            print("no Forever notes yet, nothing to send")
            return 0
        message = build_message(latest, kept, True)
        if args.test_post:
            send(message)
            print(f"test post sent: {latest['title']}")
        else:
            print(json.dumps(message, indent=2, ensure_ascii=True))
        return 0

    configured = os.environ.get("DISCORD_BOT_TOKEN") and os.environ.get("DISCORD_CHANNEL_ID")
    if not configured and state["posts"]:
        print("DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID must both be set")
        return 1
    run(posts, state, send, now)
    # Written once a day: proves it's alive and keeps the repo's schedule enabled.
    if state["last_ok"] != now.date().isoformat():
        state["last_ok"] = now.date().isoformat()
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
