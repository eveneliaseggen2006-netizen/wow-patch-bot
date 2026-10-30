import datetime
import unittest
from unittest import mock

import watcher

NOW = datetime.datetime(2026, 10, 30, tzinfo=datetime.timezone.utc)

HOTFIX = """<p>Intro text.</p>
<h2><strong>Hotfixes</strong></h2>
<p><strong><span style="color:#daa520;">October 27, 2026</span></strong></p>
<p><strong>Classes</strong></p>
<ul><li><strong>Druid</strong>
<ul><li><strong>Feral</strong>
<ul><li>All damage done increased by 5%.</li></ul></li></ul></li></ul>"""

NEWER_SECTION = """<p><strong>October 29, 2026</strong></p>
<p><strong>Classes</strong></p>
<ul><li><strong>Priest</strong>
<ul><li>All damage done increased by 5%.</li></ul></li></ul>"""


def post(content=HOTFIX, post_id=1, title="Hotfixes: October 27, 2026", published="2026-10-28T00:42:32.672Z"):
    return {"id": post_id, "slug": "hotfixes", "title": title, "url": "/news/1/hotfixes",
            "published": published, "content": content, "tags": ["hotfixes"]}


def seeded(*posts):
    state = {"last_ok": None, "posts": {}}
    with mock.patch.object(watcher, "save_state"):
        watcher.run(list(posts), state, None, NOW)
    return state


def run(posts, state):
    sent = []
    with mock.patch.object(watcher, "save_state"):
        watcher.run(posts, state, sent.append, NOW)
    return sent


class ParseTests(unittest.TestCase):
    def test_keeps_nesting(self):
        texts = [line.text for line in watcher.parse_lines(HOTFIX)]
        self.assertEqual(texts, [
            "Intro text.",
            "**Hotfixes**",
            "**October 27, 2026**",
            "**Classes**",
            "- **Druid**",
            "  - **Feral**",
            "    - All damage done increased by 5%.",
        ])

    def test_drops_table_of_contents(self):
        content = ('<h2 id="toc">TABLE OF CONTENTS</h2><table><tr><td><a href="#item1">CLASSES</a></td>'
                   '</tr></table><h2>CLASSES</h2><p><a href="#toc">Back to top</a></p>')
        self.assertEqual([line.plain for line in watcher.parse_lines(content)], ["CLASSES"])


class RunTests(unittest.TestCase):
    def test_first_run_sends_nothing(self):
        state = {"last_ok": None, "posts": {}}
        self.assertEqual(run([post()], state), [])
        self.assertIn("1", state["posts"])

    def test_unchanged_post_sends_nothing(self):
        self.assertEqual(run([post()], seeded(post())), [])

    def test_new_post_is_sent(self):
        state = seeded(post())
        sent = run([post(), post(post_id=2, title="12.2 Content Update Notes")], state)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["embeds"][0]["footer"]["text"], "New post")

    def test_update_to_same_id_sends_only_the_new_part(self):
        state = seeded(post())
        updated = post(HOTFIX.replace("<p><strong><span", NEWER_SECTION + "<p><strong><span", 1))
        description = run([updated], state)[0]["embeds"][0]["description"]
        self.assertIn("October 29, 2026", description)
        self.assertIn("Priest", description)
        self.assertNotIn("October 27, 2026", description)
        self.assertNotIn("Druid", description)

    def test_retitle_alone_sends_nothing(self):
        state = seeded(post())
        self.assertEqual(run([post(title="Hotfixes: June 3, 2026")], state), [])

    def test_formatting_change_sends_nothing(self):
        state = seeded(post())
        self.assertEqual(run([post(HOTFIX.replace("#daa520", "#ffffff"))], state), [])

    def test_edit_to_old_post_is_recorded_silently(self):
        old = post(published="2026-01-01T00:00:00Z")
        state = seeded(old)
        changed = post(HOTFIX + "<ul><li>Another change.</li></ul>", published="2026-01-01T00:00:00Z")
        self.assertEqual(run([changed], state), [])
        self.assertEqual(run([changed], state), [])

    def test_bulk_change_is_capped(self):
        state = seeded(post(post_id=99))
        posts = [post(post_id=i) for i in range(100, 120)]
        self.assertEqual(len(run(posts, state)), watcher.MAX_POSTS_PER_RUN)
        self.assertEqual(run(posts, state), [])

    def test_failed_send_is_retried_next_run(self):
        state = seeded(post())
        new = post(post_id=2)

        def fail(message):
            raise OSError("discord down")

        with mock.patch.object(watcher, "save_state"), self.assertRaises(OSError):
            watcher.run([new], state, fail, NOW)
        self.assertEqual(len(run([new], state)), 1)


class SendTests(unittest.TestCase):
    def test_posts_to_the_channel_as_the_bot(self):
        env = {"DISCORD_BOT_TOKEN": "abc", "DISCORD_CHANNEL_ID": "123"}
        with mock.patch.dict(watcher.os.environ, env), mock.patch.object(
                watcher.urllib.request, "urlopen") as urlopen:
            watcher.send({"embeds": []})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://discord.com/api/v10/channels/123/messages")
        self.assertEqual(request.get_header("Authorization"), "Bot abc")
        self.assertTrue(request.get_header("User-agent").startswith("DiscordBot ("))


class MessageTests(unittest.TestCase):
    def test_long_notes_fit_discord_limit(self):
        content = "".join(f"<ul><li>Change number {i}.</li></ul>" for i in range(2000))
        lines = watcher.parse_lines(content)
        description = watcher.build_message(post(), lines, True)["embeds"][0]["description"]
        self.assertLessEqual(len(description), 4096)
        self.assertIn("more lines in the full notes", description)

    def test_forever_is_labelled(self):
        lines = watcher.parse_lines("<ul><li>Fixed a WoW: Forever quest.</li></ul>")
        embed = watcher.build_message(post(), lines, False)["embeds"][0]
        self.assertEqual(embed["color"], watcher.COLOR_FOREVER)
        self.assertIn("WoW: Forever", embed["footer"]["text"])

    def test_only_notes_are_picked_up_from_news(self):
        ad = {"title": "Get Up to 50% In Fall Savings", "tags": []}
        forever = {"title": "World of Warcraft: Forever Hotfixes", "tags": []}
        self.assertFalse(watcher.is_notes(ad))
        self.assertTrue(watcher.is_notes(forever))


if __name__ == "__main__":
    unittest.main()
