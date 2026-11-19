<div align="center">

# ⇄ plane-github-bridge

**Plan in Plane. Build in GitHub. Never update a board by hand.**

A small, self-hosted webhook service that links [Plane](https://plane.so) Community Edition work items to GitHub issues, built by [D5-Interactive](https://github.com/D5-Interactive).

[![CI](https://github.com/D5-Interactive/plane-github-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/D5-Interactive/plane-github-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/github/license/D5-Interactive/plane-github-bridge?color=blue)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
[![Plane CE](https://img.shields.io/badge/Plane-Community%20Edition-3F76FF)](https://github.com/makeplane/plane)

[How it works](#-how-it-works) ·
[Installation](#-installation) ·
[Setup](#-setup) ·
[Linking repos](#-linking-plane-to-repos) ·
[Team workflow](#-team-workflow) ·
[Limits](#-limits) ·
[Reference](#-reference) ·
[Development](#-development)

</div>

---

## ◆ Why

Plane's built-in GitHub sync is a paid feature. This bridge gives Community
Edition the part that matters for a Scrum team:

- ▸ **Plane is where tasks are planned.** Write the task, its acceptance
  criteria and its estimate in Plane, inside a module that's mapped to a repo.
- ▸ **GitHub is where the code work happens.** The bridge creates the issue, and
  branches, commits and PRs attach to it the normal GitHub way.
- ▸ **Progress flows back on its own.** Opening, reviewing and merging PRs moves
  the work item across the sprint board.

## ◆ How it works

```mermaid
sequenceDiagram
    autonumber
    participant P as Plane
    participant B as Bridge
    participant G as GitHub

    P->>B: webhook: work item created / edited
    B->>P: fetch item and its module
    B->>G: create issue (title, description, acceptance criteria)
    B->>P: add issue link to the work item

    G->>B: webhook: PR opened / merged, issue closed
    B->>P: move work item to the matching state
    B->>P: add PR link to the work item
```

Both sides send webhooks **to the bridge**; the bridge is the only thing that
holds API keys for both. Every webhook is checked against its HMAC signature
before anything happens.

### State mapping

| On GitHub | Work item moves to |
|---|---|
| PR that says `Closes #N` opened as a draft | ◐ In Progress |
| PR ready for review | ◑ In Review |
| PR closed without merging, or converted to draft | ◐ In Progress |
| PR merged, or issue closed as completed | ● Done |
| Issue closed as not planned | ✕ Cancelled |
| Issue reopened | ○ Todo |

A PR counts as belonging to a work item if it says `Closes #N` for the linked
issue, or if its branch is named after the issue (`12-scan-command`).

### Activity feed

GitHub already shows branches, commits, PRs, reviews and merges on the issue.
The bridge copies that activity onto the Plane work item as comments, so the
same history is visible from either side:

| GitHub activity | Shows on the work item as |
|---|---|
| Branch created for the issue | ◆ comment + branch link |
| Commits pushed to the issue's branch, or mentioning `#N` | ◆ comment listing each commit |
| PR opened, ready for review, closed, reopened | ◆ comment + PR link |
| PR review: approved, changes requested, commented | ◆ comment |
| PR merged | ◆ comment, and the item moves to Done |

Each event is posted once, even if GitHub redelivers it.

### Edits

Edits to a work item's **title or description** in Plane are copied to its
issue. Edits made on the GitHub issue are **not** copied back: Plane is the
source of truth for the task itself.

## ◆ Installation

Everything runs on the Plane server, next to Plane. Clone the repo there:

```bash
cd ~
git clone https://github.com/D5-Interactive/plane-github-bridge.git
cd plane-github-bridge
```

**Requirements:**

- Docker with Compose v2
- A running Plane Community Edition instance on the same machine
- A GitHub organization (or personal account) that owns the repos to sync
- A way to give the bridge a public HTTPS address, such as a Cloudflare Tunnel
  or a reverse proxy

## ◆ Setup

The examples below use placeholder names. Replace them with your own:

| Placeholder | Means |
|---|---|
| `your-org` | Your GitHub organization (or username) |
| `plane.example.com` | The address your team uses to open Plane |
| `hooks.example.com` | The public address you give the bridge in step 5 |
| `your-workspace` | Your Plane workspace slug |

### 1. Create your config

```bash
cp .env.example .env
cp config.example.yaml config.yaml
chmod 600 .env
```

You fill in `.env` over the next steps. It holds the secrets: API tokens and
webhook secrets. Never commit it.

`config.yaml` can stay as it is. Repos are linked from inside Plane (see
[Linking Plane to repos](#-linking-plane-to-repos)); the file is only an
optional override for admins.

### 2. Plane API token

In Plane: your avatar → **Profile settings** → **Personal access tokens**
(called API tokens in some versions) → create one and copy it.

Open `.env` (`nano .env`) and paste it after `PLANE_API_KEY=`, with no quotes
or spaces:

```bash
PLANE_API_KEY=plane_api_0123456789abcdef
```

While you're in `.env`, fill in the three lines above it:

```bash
PLANE_BASE_URL=http://localhost:80              # Plane's port on this machine (LISTEN_HTTP_PORT in plane.env)
PLANE_PUBLIC_URL=https://plane.example.com      # the address your team opens Plane at
PLANE_WORKSPACE=your-workspace                  # the part after plane.example.com/ in your browser
```

> [!IMPORTANT]
> Copy `PLANE_WORKSPACE` from the address bar, not from the workspace's display
> name: in `https://plane.example.com/acme-team/projects/...` it's `acme-team`.
> A wrong slug makes every write fail with `403 You do not have permission`.

> [!NOTE]
> The bridge acts as this user. Plane's API only lets it edit projects the user
> is an explicit **member** of (role Member or Admin). Being the workspace
> owner isn't enough. Add the account to every project whose modules you'll
> link, under **Project → Settings → Members**.

### 3. GitHub token

Fine-grained tokens are created from a **user account**, then pointed at the
org. Sign in as the account the issues should come from: your own, or a
dedicated bot account if you'd rather the issues not show your name.

Create the token at <https://github.com/settings/personal-access-tokens/new>
(your avatar → **Settings → Developer settings → Personal access tokens →
Fine-grained tokens**):

| Setting | Value |
|---|---|
| Resource owner | `your-org` (not your username) |
| Repository access | **All repositories**, or only the repos you'll link |
| Permissions | **Issues → Read and write** (Metadata → Read is added automatically) |

That one permission is enough. (The automatic Metadata access lets the bridge
read a repo's description for the module header.) Commits, branches, PRs and
reviews reach the
bridge inside GitHub's webhook payloads (step 7), so the token never reads
them. Keeping it to Issues means a leaked token can't touch your code.

**All repositories** means linking a new repo is done entirely in Plane. With
**Only select repositories**, each new repo also has to be added to the token
before its issues can be created.

> [!NOTE]
> If `your-org` doesn't appear under Resource owner, an org owner has to allow
> fine-grained tokens first: **Org settings → Personal access tokens →
> Settings**. If the org requires approval, the owner then approves the token
> under **Pending requests**.

Then make up a secret for the GitHub webhook:

```bash
openssl rand -hex 32
```

Put both in `.env`, along with your org name:

```bash
GITHUB_TOKEN=github_pat_0123456789abcdef
GITHUB_WEBHOOK_SECRET=<the output of openssl rand -hex 32>
GITHUB_ALLOWED_OWNERS=your-org
```

`GITHUB_ALLOWED_OWNERS` limits which GitHub owners anyone in Plane can link to.
Separate several with commas; leave it empty to allow any repo the token can
reach.

Leave `PLANE_WEBHOOK_SECRET` empty for now; Plane gives you that one in step 6.
Until then the bridge starts but turns away Plane webhooks.

### 4. Start the bridge

```bash
docker compose up -d --build
```

The first build takes a minute or two. Give the bridge a few seconds to boot,
then check it:

```bash
sleep 5
curl http://127.0.0.1:8095/health
```

You should see:

```json
{"ok":true,"mappings":0,"plane_webhook_ready":false,"plane_api_ok":true}
```

`mappings` counts admin overrides in `config.yaml`, so `0` is normal.
`plane_api_ok` is checked once at startup: `false` comes with a
`plane_problem` message saying what's wrong (usually the workspace slug or the
API token). `plane_webhook_ready`
stays `false` until step 6. The log also warns that `PLANE_WEBHOOK_SECRET` is not
set; that's expected for now.

If `curl` can't connect, check that the container is up and read its log:

```bash
docker compose ps                    # STATUS should be "Up", not "Restarting"
docker compose logs --tail 30 bridge
```

| Log says | Fix |
|---|---|
| `Missing required environment variable ...` | That line in `.env` is empty or misspelled |
| `IsADirectoryError` or a YAML error | `config.yaml` is missing, so Docker created a folder in its place. Run `docker compose down && rmdir config.yaml && cp config.example.yaml config.yaml`, fill it in, then start again |

### 5. Give it a public address

Plane and GitHub need to reach the bridge over HTTPS. Point a hostname such as
`hooks.example.com` at `http://localhost:8095` on this machine.

**With a Cloudflare Tunnel:** Zero Trust → **Networks → Tunnels** → your tunnel
→ **Add route → Published application**:

| Field | Value |
|---|---|
| Subdomain | `hooks` |
| Domain | `example.com` |
| Path | leave empty |
| Service URL | `http://localhost:8095` |

**With a reverse proxy** such as Caddy:

```
hooks.example.com {
    reverse_proxy 127.0.0.1:8095
}
```

Check it from a **different machine**, such as your laptop:

```bash
curl https://hooks.example.com/health
```

You should get the same `{"ok":true,...}` as in step 4.

> [!TIP]
> The bridge only listens on `127.0.0.1`, so your tunnel or proxy is the only
> way in. Only `/plane/webhook`, `/github/webhook` and `/health` exist, and both
> webhooks reject anything without a valid signature.

### 6. Plane webhook

1. In Plane, open **Workspace settings → Webhooks → Add webhook**.
2. Fill in:

   | Field | Value |
   |---|---|
   | URL | `https://hooks.example.com/plane/webhook` |
   | Events | **Work items** (called Issues in some versions) and **Modules**. Projects, cycles and comments aren't needed |

3. Save. Plane shows a secret that starts with `plane_wh_`. Copy it; Plane may
   not show it again.
4. On the server, open `.env` and paste it in:

   ```bash
   nano .env
   ```

   ```bash
   PLANE_WEBHOOK_SECRET=plane_wh_0123456789abcdef
   ```

5. Recreate the container so it reads the new value. A plain `restart` does not
   reload `.env`:

   ```bash
   docker compose up -d --force-recreate
   sleep 5 && curl http://127.0.0.1:8095/health
   ```

   `plane_webhook_ready` should now be `true`.

### 7. GitHub webhook

One webhook on the organization covers every repo, including ones you add
later. If you aren't an org owner, add the same webhook to each mapped repo
under **Repo → Settings → Webhooks** instead.

1. On the server, print the webhook secret you made in step 3:

   ```bash
   grep '^GITHUB_WEBHOOK_SECRET=' .env
   ```

   GitHub doesn't generate this one; you give it the same value the bridge has.

2. On GitHub, open **your-org → Settings → Webhooks → Add webhook** and fill in:

   | Field | Value |
   |---|---|
   | Payload URL | `https://hooks.example.com/github/webhook` |
   | Content type | `application/json` (the default is form-encoded; change it) |
   | Secret | everything after `GITHUB_WEBHOOK_SECRET=` from step 1 |
   | SSL verification | Enable SSL verification (the default) |

3. Under **Which events would you like to trigger this webhook?**, choose **Let
   me select individual events**. A long checklist opens. **Push** is ticked by
   default; leave it ticked, and make sure exactly these five are on:

   - ✓ Branch or tag creation
   - ✓ Issues
   - ✓ Pull requests
   - ✓ Pull request reviews
   - ✓ Pushes

   Leave every other box unticked. "Send me everything" also works, since the
   bridge ignores events it doesn't use, but it sends your server a lot of
   pointless traffic.

4. Keep **Active** ticked and click **Add webhook**.
5. Open the webhook again and select the **Recent Deliveries** tab. GitHub sends
   a ping as soon as the webhook is created; it should show a green ✓.

| Ping shows | Meaning |
|---|---|
| ✓ 202 | Working (202 means "accepted") |
| ✕ 401 | The secret doesn't match `.env`. Paste it again, or make a new one in both places |
| ✕ couldn't connect / 502 | GitHub can't reach the bridge. Recheck step 5 |

### 8. Try it

Link a module to a repo as described in
[Linking Plane to repos](#-linking-plane-to-repos), then create a work item in
that module. Within a few seconds a GitHub issue
appears and its link shows on the work item.

```bash
docker compose logs -f bridge
```

> [!IMPORTANT]
> If nothing happens, set `LOG_LEVEL=DEBUG` in `.env`, restart, and try again.
> The log then shows each Plane payload exactly as received.

## ◆ Linking Plane to repos

Only work items in a **linked module** go to GitHub. Everything else stays in
Plane, which keeps admin, planning and other organizational work off GitHub:

| Work item is in | Goes to GitHub? |
|---|---|
| A module whose description has a GitHub repo link | ✓ Yes, to that repo |
| A module with no repo link | ✕ No, stays in Plane |
| No module at all | ✕ No, stays in Plane |

Anyone who can edit a module in Plane can link it. No server access is needed.

1. In Plane, open the project → **Modules** → the module (or create one).
2. Paste the repo's GitHub URL **anywhere** in the module's description, for
   example `code is at https://github.com/your-org/auth-service, ask Sam`.
3. Save. Within a few seconds the bridge tidies the module, which is also how
   you can tell the link worked:
   - **Name** becomes the repo's name, e.g. `auth-service`.
   - **Links** gets the repo, titled `GitHub: your-org/auth-service` (see the
     note below on Plane versions).
   - **Start date** becomes the day the repo was created on GitHub.
   - **Description** becomes the repo link and the repo's GitHub description:

     ```
     https://github.com/your-org/auth-service
     Login, sessions and token refresh for the web app.
     ```

   If the repo has no description on GitHub, only the link is kept. To change
   that second line, edit the repo's **About** on GitHub, then save the module
   again.
4. That's it. Work items created in that module get an issue within seconds.
   Existing items moved into the module get theirs within about two minutes
   (see [Catch-up sync](#catch-up-sync)).

> [!NOTE]
> The tidy-up replaces everything else in the module's name and description, so
> keep notes in Plane Pages or the work items instead. If another module in the
> project already has the repo's name, only the description is updated. Nothing
> changes if the GitHub token can't see the repo or its owner isn't in
> `GITHUB_ALLOWED_OWNERS`.

Changes take effect within a minute. To unlink a module, remove the URL from
its description; issues that already exist stay linked.

> [!NOTE]
> Admins can also pin module mappings in `config.yaml`, which take priority over
> links set in Plane. See the comments in `config.example.yaml`. Mappings there
> match the module by name, so update them if the module is renamed.

## ◆ Team workflow

1. **Plan in Plane.** Create the task in a linked module and write the
   acceptance criteria as a checklist; it becomes a checklist on the GitHub issue.
2. **Branch from the issue.** On the GitHub issue: **Development → Create a
   branch**. GitHub names it like `12-scan-command`, which links the branch, its
   commits and its PR to the work item automatically.
3. **Close it from the PR.** Put `Closes #<issue number>` in the PR description.
   Commits on other branches show up too if their message mentions `#12`.
4. **Hands off the board.** Leave the Plane state alone; the bridge moves it.

## ◆ Limits

- Only work items in linked modules sync. Work items in unlinked modules or in
  no module stay in Plane. Moving an item into a linked module later creates its
  issue at the next catch-up sync, within about two minutes.
- Deleting a work item doesn't delete or close its issue.
- A PR only moves a work item if it says it closes the linked issue or its
  branch is named after it.
- Commit and review comments only reach Plane for issues the bridge created.
- State names should be **Todo, In Progress, In Review, Done** and
  **Cancelled**. Otherwise the bridge falls back to the first state in the
  matching state group.

## ◆ Reference

The exact rules the bridge follows. Everything here matches the code in
`bridge/`; if you change a rule, update this section with it.

### Events it listens to

| Source | Event | What the bridge does |
|---|---|---|
| Plane | Work item created / updated | Creates the GitHub issue if the item is in a linked module and has none yet; otherwise pushes title/description edits to its issue |
| Plane | Work item deleted | Nothing. The issue stays |
| Plane | Module created / updated | Tidies the module if its description links a repo |
| GitHub | `issues` closed / reopened | Moves the work item's state |
| GitHub | `pull_request` | Moves the state, adds the PR link and an activity comment |
| GitHub | `pull_request_review` submitted | Activity comment |
| GitHub | `push` | Activity comment listing the commits |
| GitHub | `create` (branch) | Activity comment and branch link |
| GitHub | `ping` | Replies so GitHub shows a green ✓ |
| Either | Anything else | Ignored and logged |

Every request is checked against its HMAC-SHA256 signature first:
`X-Plane-Signature` with `PLANE_WEBHOOK_SECRET`, `X-Hub-Signature-256` with
`GITHUB_WEBHOOK_SECRET`. A bad or missing signature gets `401` and nothing else
happens. A good one gets `202 Accepted` at once and is processed in the
background, so Plane and GitHub never wait on the bridge.

### Catch-up sync

Plane sends **no webhook** when an existing work item is added to a module.
Without help, an item created first and then dragged into a linked module would
never reach GitHub. So every `BRIDGE_SYNC_INTERVAL` seconds (default 120, and
once at startup) the bridge:

1. adds any issue links that never reached Plane (up to 10 per pass, least
   recently tried first; see below),
2. lists the workspace's projects and their modules,
3. picks the modules linked to a repo (description URL or `config.yaml`) whose
   owner is allowed; projects with none are skipped without listing anything,
4. lists each linked module's items once, and creates an issue for every item
   that doesn't have one yet.

Items that already have an issue are skipped, so it never creates duplicates.
Projects the token's account can't read are skipped quietly. A pass costs one
Plane API call for the project list, one per project for its modules, one per
linked module, and a few per new item, so a quiet pass stays cheap however many
items your modules hold. Set `BRIDGE_SYNC_INTERVAL=0` to turn it off.

When something goes wrong:

| What happens | What the pass does |
|---|---|
| One item fails | Logs one line with the project and item ID and the reason, then carries on with the rest |
| GitHub refuses the item for a lasting reason (repo gone, Issues disabled, no access: 403, 404, 410, 422) | Parks it for that repo and skips it for 6 hours, logging one line per repo. Editing the item in Plane, linking its module to a different repo, or restarting the bridge retries it at once |
| Any other error (Plane errors other than 429 and 401, GitHub 5xx, timeouts, network) | Retries the item next pass |
| Plane or GitHub rate-limits the bridge (429, or GitHub's secondary rate limit), anywhere in the pass | Stops the pass at once instead of hammering the API; the next pass picks up where it left off |
| Plane or GitHub rejects the key or token (401) | Stops the pass with one log line; nothing is parked, so fixing the credential and waiting a pass is enough |
| The issue was created but adding its link to the Plane item failed | The link is recorded as missing and added by a later pass, so the issue is never created twice. If the work item has since been deleted (Plane answers 404), the bridge stops trying |

The log gets one summary line per pass, and only when the pass did something.

The catch-up sync only creates **missing issues** and adds **missing issue
links**. It doesn't replay other work: a title or description edit, or a state
change from GitHub, whose webhook failed (for example because Plane still
answered 429 after the retries) is lost, and the log says so. Editing the item
again sends the edit again.

### Deciding whether a work item goes to GitHub

When a work item without an issue is created or updated, the bridge looks for a
repo in this order and stops at the first match:

1. **`config.yaml`** mappings for the item's project, if the item is in that
   mapping's module.
2. **Module descriptions:** each module in the project whose description holds a
   `https://github.com/owner/repo` URL, if the item is in that module.
3. **No match:** the item stays in Plane. The log says
   `kept in Plane: item isn't in a module linked to a repo`.

Then two last checks: the repo's owner must be in `GITHUB_ALLOWED_OWNERS` (when
that's set), and the token must be able to create issues there.

Details:

- The URL can be anywhere in the description, in plain text or as a link.
  `.git` endings and trailing paths like `/issues/3` are ignored. The first
  GitHub repo URL wins.
- Project descriptions are **not** read. An item in no module never syncs.
- An item in several linked modules goes to the first one Plane lists.
- Module and project details are cached for 60 seconds, so a new link takes up
  to a minute to apply.
- An item gets at most one issue, ever. Moving it to another module afterwards
  doesn't create a second issue or move the existing one.

### What the GitHub issue contains

| Issue field | Comes from |
|---|---|
| Title | The work item's name |
| Body | The work item's description, converted to GitHub Markdown |
| Footer | `Synced from Plane work item [ALL-12](link). Plan, estimate and edit the task in Plane; do the code work here.` |

Plane checklists become GitHub task lists: an unchecked item is `- [ ]`,
a checked one `- [x]`. Headings, lists, bold, links and code carry over.

The work item gets a link back, titled `GitHub owner/repo#12`.

### Edits after the issue exists

- **Plane → GitHub:** when the work item's **title or description** changes, the
  bridge rewrites the issue's title and body from the work item. Other fields
  (assignees, labels, priority, estimate, dates) aren't copied.
- **GitHub → Plane:** edits to the issue's title or body are not copied back,
  and the next Plane edit overwrites them. Treat Plane as the source of truth
  for the task's wording.

### How PRs, branches and commits find their work item

GitHub activity is matched to a work item through the issue the bridge created.
Activity on any other issue is ignored.

| Activity | Matched when |
|---|---|
| Pull request | Its title or body says `close`, `closes`, `closed`, `fix`, `fixes`, `fixed`, `resolve`, `resolves` or `resolved` followed by `#12` (or `owner/repo#12`), **or** its branch is named after the issue |
| Branch | Its name starts with the issue number: `12-scan-command`, `12_scan`, `feature/12-scan` |
| Commit | It's pushed to a branch named after the issue, **or** its message mentions `#12` anywhere |
| Review | It's on a PR that matches, by the PR rules above |

### State changes

| On GitHub | Work item moves to |
|---|---|
| PR opened as a draft, converted to draft, or closed without merging | In Progress |
| PR opened (not draft), marked ready, reopened, edited, or new commits pushed to it | In Review |
| PR merged | Done |
| Issue closed as completed | Done |
| Issue closed as not planned | Cancelled |
| Issue reopened | Todo |

The bridge finds each target state by **name** first (`Todo`, `In Progress`,
`In Review`, `Done`, `Cancelled`, case-insensitive) and otherwise takes the
first state in the matching Plane state group (unstarted, started, completed,
cancelled). With no `In Review` state, PRs in review land in the first
"started" state. If the work item is already in the target state, nothing is
sent.

### Activity comments

Comments are posted by the Plane account that owns `PLANE_API_KEY`, and start
with `◆ GitHub ·`.

| Activity | Comment | Also adds a link |
|---|---|---|
| Branch created | `branch 12-scan created by zep` | The branch |
| Push | `3 commits pushed to 12-scan by zep`, then each commit's short hash (linked) and first line, up to 10, then "…and N more" | ✕ |
| PR opened / ready / converted / closed / reopened / merged | `PR repo#5 "Add scan" merged into main by zep` | The PR, the first time it's seen |
| Review | `lead ✓ approved PR repo#5`, `✕ requested changes on`, or `commented on` (empty review comments are skipped) | ✕ |

Each one is posted once. The bridge records what it has posted for each work
item (each commit, branch, review and PR event), so a commit is listed only the
first time it's pushed, and a redelivered GitHub event posts nothing new. Commit messages and
names are HTML-escaped, so nothing in a commit can inject markup into Plane.

### Module tidy-up

Runs when Plane reports a module was created or updated, and whenever a work
item is synced through a module. If the module's description links a repo that
the token can see and the owner is allowed:

1. **Name** → the repo's name (`auth-service`). If another module in the
   project already has that name, Plane refuses the rename and the rest still
   happens.
2. **Description** → the repo link, then the repo's GitHub description (its
   **About** text). No About text, just the link.

3. **Start date** → the day the repo was created on GitHub (its `created_at`,
   in UTC). This is the repo's creation date, not its first commit: reading
   commits would need a token that can read code. For a repo created on GitHub
   they're normally the same day; for one pushed from older local history,
   the first commit can be earlier. Plane refuses a start date after the
   module's target date; then the start date is left as it was.
4. **Links** → the repo URL, titled `GitHub: owner/repo`, added once. Plane's
   public API doesn't document module links, so this is best effort: if your
   Plane version doesn't accept it, the bridge logs
   `this Plane's API has no module links endpoint` once, stops trying, and the
   renamed module and its description serve as the confirmation instead.

The comparison is on the visible text, so the `module.updated` webhook caused by
the bridge's own edit changes nothing and can't loop. Anything else that was in
the description is replaced.

### What's stored

A SQLite database at `/data/bridge.db`, in the `bridge-data` Docker volume:

| Table | Holds |
|---|---|
| `links` | Work item ↔ GitHub issue pairs, and whether the issue link reached the Plane item. This is what makes "one issue per item" work |
| `seen_events` | Plane event IDs, to drop Plane's retries |
| `posted` | Which commits, branches, reviews and PR events were already posted per item |
| `pr_links` | Which PR links were already added per item |
| `sync_failures` | Items GitHub refused for a lasting reason, with the repo and when, so the catch-up sync waits before retrying them. Cleared at every startup |

> [!WARNING]
> Deleting the volume (`docker compose down -v`) forgets every link. The next
> edit to an already-synced work item would then create a **second** GitHub
> issue. Back the volume up with the rest of your server.

#### Upgrading and rolling back

Upgrading migrates the database by itself: the `links` table gains the
`plane_linked` and `last_link_attempt` columns, and `sync_failures` is created
or rebuilt. Rows that existed before the upgrade count as linked in Plane.

**Rolling back** to a version from before these columns existed is **not
safe** as it is: the older code saves links with five values, the table now has
seven columns, and every save fails *after* the GitHub issue was created, so
each event makes another duplicate issue. If you must roll back, stop the
bridge, back up the database, and remove the new columns first (SQLite 3.35 or
newer):

```bash
docker compose stop bridge
docker run --rm -v plane-github-bridge_bridge-data:/data alpine sh -c \
  'apk add --no-cache sqlite >/dev/null && cp /data/bridge.db /data/bridge.db.bak && \
   sqlite3 /data/bridge.db "ALTER TABLE links DROP COLUMN last_link_attempt; \
     ALTER TABLE links DROP COLUMN plane_linked; DROP TABLE IF EXISTS sync_failures;"'
```

Then check out the older version and start it as usual. The volume name is
`<folder>_bridge-data`; `docker volume ls` shows yours.

**Re-checking issue links.** Links lost before this version (when a pass died
between creating the issue and adding its link) aren't known to the database.
To have the bridge check every link once, set `BRIDGE_RECHECK_LINKS=1` in
`.env` and recreate the container. At startup it marks every link as
"maybe missing", and the catch-up sync re-adds them 10 per pass. Plane refuses
a link URL an item already has, so items that kept their link are left as they
are. When the log stops showing `sync: repaired ...` lines, remove the setting
and recreate the container again, or the next restart re-checks everything.

### Security model

- The bridge listens on `127.0.0.1:8095` only; a tunnel or reverse proxy is the
  only way in.
- Both webhooks need a valid HMAC signature. An unset `PLANE_WEBHOOK_SECRET`
  rejects every Plane webhook with `503` rather than accepting unsigned ones.
- The GitHub token needs only **Issues: Read and write** (plus the automatic
  **Metadata: Read**). It can't read or change code.
- `GITHUB_ALLOWED_OWNERS` stops anyone in Plane from pointing a module at a repo
  outside your org.
- The container runs as an unprivileged user.

### Reading the log

`docker compose logs -f bridge` prints one line per event:

| Log line | Meaning |
|---|---|
| `plane: created owner/repo#12` | New issue made for a work item |
| `plane: kept in Plane: ...` | The item isn't in a linked module; working as intended |
| `sync: created owner/repo#12, ...` | The catch-up sync found items added to linked modules and made their issues |
| `sync: repaired 2 Plane link(s)` | Issue links that didn't reach Plane earlier were added |
| `sync: API item ... failed, retrying next pass (...)` | One item hit a temporary error; the rest of the pass went on |
| `sync: owner/repo refused items from API (...); parked for 6h ...` | GitHub refused items for a lasting reason, such as Issues being disabled or the token lacking access. Fix it and edit the item, or restart the bridge |
| `sync: stopped early, will resume next pass (... answered 429 ...)` | Plane or GitHub rate-limited the bridge; the catch-up sync loses nothing |
| `sync: stopped, a key or token was rejected (...)` | `GITHUB_TOKEN` or `PLANE_API_KEY` is wrong or expired; the sync tries again every pass |
| `sync: work item ... is gone from Plane; not adding the link ...` | The item was deleted after its issue was made; harmless |
| `sync: cleared 3 parked item(s); they'll be tried again` | Startup: items parked before the restart get a fresh try |
| `BRIDGE_RECHECK_LINKS is set: re-checking ...` | The one-off link re-check is on; remove the setting when it's done |
| `catch-up sync of linked modules every 120s` | Startup message; the sync is on |
| `plane: updated owner/repo#12` | Title/description edit pushed to the issue |
| `plane: module tidied for owner/repo` | Module renamed, description rewritten and/or repo link added |
| `this Plane's API has no module links endpoint` | Your Plane can't take module links from the API; name and description still update |
| `plane: unchanged: module already tidy` | The bridge's own edit coming back; harmless |
| `plane: ignored: token can't see owner/repo` | Add the repo to the GitHub token, or fix the URL |
| `plane: ignored: ... GITHUB_ALLOWED_OWNERS` | The module links a repo outside the allowed owners |
| `plane: ignored: duplicate delivery` | Plane retried an event already handled |
| `github:push: ignored: commits don't touch a linked issue` | A push unrelated to any synced work item |
| `github:pull_request: moved to review` | State changed on the work item |
| `... failed` followed by a traceback | Something errored; the event is skipped and the bridge keeps running |
| `WARNING PLANE_WEBHOOK_SECRET is not set` | Expected until setup step 6 is done |
| `PLANE SETUP PROBLEM: Plane refused workspace '...'` | `PLANE_WORKSPACE` doesn't match the slug in your Plane URL |
| `... -> 403: You do not have permission to perform this action.` | The token's account isn't a member of that project, or the slug is wrong |
| `Plane rate limit: GET /... answered 429; waiting 10s (retry 1 of 3)` | Plane's per-key limit was hit; the bridge waits and retries. If it's frequent, lower `PLANE_RATE_LIMIT` |
| `GitHub rate limit: POST /repos/... answered 403; waiting 60s and retrying once` | GitHub's secondary rate limit, usually from creating many issues quickly; harmless |

Set `LOG_LEVEL=DEBUG` in `.env` and recreate the container to also print each
Plane payload as received.

### API usage

Per Plane event the bridge makes a handful of Plane API calls (the work item,
plus the project and module list when they're not cached) and at most one or two
GitHub calls.

**Plane's rate limit.** Plane limits each API key to a number of requests per
minute (60 by default) and answers `429 RATE_LIMIT_EXCEEDED` above it. The
bridge keeps itself under the limit and recovers when it hits it anyway:

- **Pacing.** All Plane calls are spaced evenly so they stay under
  `PLANE_RATE_LIMIT` requests per minute (default `50`). Set it a little below
  your Plane's limit, or to `0` to turn pacing off.
- **Backoff.** A request answered `429` waits as long as Plane's `Retry-After`
  (or `X-RateLimit-Reset`) header says, at most 60 seconds, then is sent again,
  up to 3 times. Every other request waits out the same window, so one 429
  doesn't turn into many. If Plane asks for more than 60 seconds, the bridge
  doesn't wait at all and fails the request at once. A failed request is
  logged; the catch-up sync retries missing issues and links on its next pass,
  but a webhook's edit or state change isn't replayed (edit the item again).

**GitHub's rate limits.** A `429`, or a `403` that is GitHub's secondary rate
limit, is retried once after the wait GitHub asks for, if that's 60 seconds or
less; a longer wait (such as the hourly limit) fails at once. Ordinary `403` and
`404` answers aren't retried.

**Error logs.** A failed request is logged as its method, path and status only.
Response bodies can echo work item content, so they're only logged with
`LOG_LEVEL=DEBUG`.

## ◆ Development

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.14.

```bash
uv sync            # create .venv with all dependencies
uv run pytest      # run the tests
uv add <package>   # add a dependency (updates pyproject.toml and uv.lock)
```

Run it locally without Docker:

```bash
cp .env.example .env && cp config.example.yaml config.yaml   # then fill them in
set -a && . ./.env && set +a
BRIDGE_CONFIG=config.yaml BRIDGE_DB=bridge.db \
  uv run uvicorn bridge.app:create_app --factory --port 8095
```

### Project layout

```
bridge/
├── app.py       # FastAPI app: webhook endpoints + signature checks
├── sync.py      # the sync rules (Plane → GitHub, GitHub → Plane)
├── clients.py   # Plane and GitHub REST clients
├── render.py    # HTML → Markdown, closing-reference parsing, signatures
├── store.py     # SQLite: item ↔ issue links, delivery de-duplication
└── config.py    # environment settings + optional config.yaml overrides
tests/           # pytest suite with fake Plane and GitHub APIs
```

## ◆ License

[MIT](LICENSE) © D5i
