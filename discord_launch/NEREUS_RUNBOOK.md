# Nereus — Venice announcements

Nereus is a third, isolated Discord app. Triton retains license/purchase roles;
Venice Guard retains security/tickets. Nereus never receives those secrets or
permissions.

## Current guild wiring

- Guild: `1483836452776316970`
- Public, read-only `#announcements`: `1549859272341332111` under Community.
- Private audit output: `#audit-log` `1549499828390727732`.
- Admin role: `1485348271638450449`.
- `/announce` is Admin/owner-only, invoked in `#announcements`; its first response
  is an ephemeral preview. Only the original Admin can press Publish. Nereus
  writes an audit request before posting and blocks the post if audit fails.
- Announcement text cannot contain user-supplied links or ping roles/everyone.
  The only link is the fixed Venice website button.

## Create and invite the app

1. In the Discord Developer Portal, create an application named **Nereus** and
   add its bot user. Leave Interactions Endpoint URL empty so the gateway
   process receives slash commands. No privileged gateway intents are needed.
2. Give it the blue Venice V icon (`C:\Users\aaron\Desktop\NexusVision\assets\orion.png`).
3. Before inviting the bot, run
   `python .codex_artifacts/nereus-announcements-20260916/deploy_nereus.py --prepare-bot-id <NEREUS_APP_ID>`.
   This lets Venice Guard accept the bot's join without exempting it from
   anti-nuke audit. Verify the guard service remains active.
4. Install to the Venice guild using the `bot` and `applications.commands`
   scopes with **zero guild-wide permissions**. Nereus gets only channel-level
   member overwrites (Discord created no managed bot role for this zero-permission
   install). Never grant Administrator, Manage Roles, Manage Channels,
   Manage Webhooks, Kick, Ban, or Mention Everyone.
5. Store its token as an encrypted AWS SSM SecureString named
   `/orion/nereus_bot_token`. Never put the token in the repo, Discord, or chat.
6. Run `python .codex_artifacts/nereus-announcements-20260916/deploy_nereus.py --activate`
   after the bot is installed. It verifies the bot, grants only channel-level
   access, installs the existing-EC2 systemd service, and checks slash-command
   registration. No additional server or monthly hosting plan is required.

## Role/channel permissions

| Surface | Nereus member allow | Nereus member deny |
|---|---|---|
| Guild-wide | none | Administrator, management and moderation remain absent |
| `#announcements` | View, Send, Embed Links, Read History | Mention Everyone absent |
| `#audit-log` | View, Send | Read History |
| Other channels | none | inherits existing restrictions |

## Operator workflow

An Admin runs `/announce title:<headline> body:<copy> kind:<Update|Maintenance|Incident>`
in `#announcements`. The preview is visible only to them. They press **Publish
announcement** to send the public embed; Cancel leaves the channel unchanged.
Nereus logs actor ID, kind, title, content fingerprint, and published message ID.

## Verify

- `systemctl is-active nereus.service` must return `active`.
- `/announce` must be registered with Manage Server default permission.
- A non-Admin must not see/use the command; an Admin must receive an ephemeral
  preview. No public message appears until Publish.
- A test publish must create the audit request first and a single public embed.
- `#announcements` remains visible/read-only to everyone.

## Credential rotation

Stop/disable `nereus.service` first. The owner resets the Nereus Bot token in
Discord and copies it to the local clipboard without sending it in chat. Run
`.venv\Scripts\python.exe .codex_artifacts\nereus-announcements-20260916\rotate_nereus_token.py`.
The script verifies the new bot identity, requires the old token to return 401,
replaces the AWS SSM SecureString, verifies readback, restarts the service, and
clears the clipboard. Leave the service stopped if any check fails.
