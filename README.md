# plane-github-bridge

Links D5i's Plane work items to GitHub issues.

- **Plane is where tasks are planned.** Create a Scrum task in Plane, write the
  description and acceptance criteria (use a checklist), and put it in a module
  that's mapped to a repo. The bridge creates the GitHub issue and adds its link
  to the work item.
- **GitHub is where the code work happens.** Branches, commits and PRs attach to
  that issue the normal GitHub way.
- **Progress flows back to Plane automatically.**

| On GitHub | Work item moves to |
|---|---|
| PR that says `Closes #N` opened (draft) | In Progress |
| PR ready for review | In Review |
| PR closed without merging, or converted to draft | In Progress |
| PR merged, or issue closed as completed | Done |
| Issue closed as not planned | Cancelled |
| Issue reopened | Todo |

The PR link is also added to the work item. Edits to the work item's title or
description in Plane are copied to the issue. Edits made on the GitHub issue are
**not** copied back; Plane is the source of truth for the task itself.

## Setup

Everything runs on the Plane server, next to Plane.

### 1. Copy and configure

```bash
cd ~/plane-github-bridge
cp .env.example .env
cp config.example.yaml config.yaml
chmod 600 .env
```

Edit `config.yaml` to map Plane projects/modules to repos.

### 2. Plane API token

In Plane: your avatar → **Profile settings** → **Personal access tokens**
(called API tokens in some versions) → create one → paste into `PLANE_API_KEY`.

The bridge acts as this user, so use an account that's a member of every mapped
project.

### 3. GitHub token

Create a fine-grained token at
<https://github.com/settings/personal-access-tokens/new>:

- **Resource owner:** D5-Interactive
- **Repository access:** only the repos in `config.yaml`
- **Permissions:** Issues → Read and write (Metadata → Read is added automatically)

Paste it into `GITHUB_TOKEN`. If the org requires approval for fine-grained
tokens, an owner approves it under **Org settings → Personal access tokens →
Pending requests**. Issues the bridge creates show as opened by the token's owner.

Put a long random string in `GITHUB_WEBHOOK_SECRET` (`openssl rand -hex 32`).

### 4. Start it

```bash
docker compose up -d --build
curl http://127.0.0.1:8095/health      # {"ok":true,"mappings":1}
```

### 5. Give it a public address

Cloudflare Zero Trust → **Networks → Tunnels** → your tunnel → **Add route →
Published application**:

- Subdomain `hooks`, domain `d5i.dev`
- Service URL `http://localhost:8095`

Only the two webhook paths and `/health` exist; both webhooks reject anything
without a valid signature.

### 6. Plane webhook

Plane → **Workspace settings → Webhooks → Add webhook**:

- URL: `https://hooks.d5i.dev/plane/webhook`
- Events: work items (issues). Projects, cycles, modules and comments aren't needed.

Copy the secret it shows (starts `plane_wh_`) into `PLANE_WEBHOOK_SECRET`, then
`docker compose up -d` to restart with it.

### 7. GitHub org webhook

GitHub → **D5-Interactive → Settings → Webhooks → Add webhook**:

- Payload URL: `https://hooks.d5i.dev/github/webhook`
- Content type: `application/json`
- Secret: the `GITHUB_WEBHOOK_SECRET` value
- Events: **Let me select** → **Issues** and **Pull requests**

GitHub sends a ping right away; it should show a green check.

### 8. Try it

Create a work item in the mapped module. Within a few seconds a GitHub issue
appears and its link shows on the work item.

```bash
docker compose logs -f bridge
```

If nothing happens, set `LOG_LEVEL=DEBUG` in `.env`, restart, and repeat: the
log then shows each Plane payload as received.

## How the team uses it

1. Create the task in Plane, in the right module. Write acceptance criteria as a
   checklist; it becomes a checklist on the GitHub issue.
2. Start work from the GitHub issue: **Development → Create a branch**, so the
   branch is linked to the issue.
3. Open the PR with `Closes #<issue number>` in the description.
4. Leave the Plane state alone; the bridge moves it.

## Limits

- Only work items in mapped projects/modules sync. Adding an item to a mapped
  module later also creates the issue.
- Deleting a work item doesn't delete or close its issue.
- A PR only moves a work item if it says it closes the linked issue.
- The bridge needs the Plane states to be named Todo, In Progress, In Review,
  Done and Cancelled; otherwise it falls back to the first state in the matching
  state group.

## Development

The project uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync            # creates .venv with Python 3.14 and all dependencies
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
