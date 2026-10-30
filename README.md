# WoW: Forever Patch Notes Bot

Posts World of Warcraft: Forever patch notes and hotfixes to a Discord channel. Other
versions of WoW are ignored, and shared hotfix posts are cut down to their Forever
sections. GitHub Actions runs it every 15 minutes for free, so your PC doesn't need
to be on.

It reads Blizzard's [content update notes](https://worldofwarcraft.blizzard.com/en-us/content-update-notes)
and [news](https://worldofwarcraft.blizzard.com/en-us/news) pages. When a hotfix post
gets new changes, only the new changes are posted.

## Setup

1. At the [Discord Developer Portal](https://discord.com/developers/applications), click
   New Application and name it. This name is the bot's name.
2. On the Bot page, click Reset Token and copy the token. Treat it like a password:
   anyone who has it controls the bot.
3. On the OAuth2 page, in URL Generator, tick the `bot` scope, then the View Channels,
   Send Messages and Embed Links permissions. Open the generated URL to add the bot to
   your server.
4. In Discord: User Settings > Advanced > turn on Developer Mode. Right-click your
   channel and click Copy Channel ID.
5. Put this folder in a new **public** GitHub repo. Private repos only get 2,000 free
   minutes a month, which this schedule would exceed.
6. In the repo: Settings > Secrets and variables > Actions. Add two repository secrets,
   `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID`.
7. In the Actions tab, enable workflows if asked, open **Watch WoW patch notes**, and
   click **Run workflow**. A green check means it's working.

The bot shows as offline in the member list, because it only wakes up to check and
post. Posting still works normally.

## Commands

```bash
python watcher.py --dry-run
```

Prints what the latest Forever post would look like. Changes nothing.

```bash
python watcher.py --test-post
```

Sends the latest Forever post to Discord, if there is one. Needs `DISCORD_BOT_TOKEN`
and `DISCORD_CHANNEL_ID` set.

```bash
python -m unittest test_watcher
```

Runs the tests.

## If it stops working

GitHub emails you when a run fails. That happens if Blizzard's page layout changes or
their site can't be reached for about 2 days. Short outages are retried quietly, and
anything posted during one is picked up on the next successful run.
