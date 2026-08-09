# Hermes Containers

A `docker` shim that runs Hermes Agent on **Apple Container** instead of Docker,
with **one container per Hermes project**, each with its own project directory
bind-mounted at `/workspace`.

## The problem

Hermes shells out to `docker`. Apple's `container` CLI is close but not
compatible, and two gaps matter:

1. **Container sprawl.** Hermes reuses containers per `(task_id, profile)` by
   probing `docker ps -a --filter label=hermes-task-id=<id> --format "{{.ID}}\t{{.State}}"`
   (`tools/environments/docker.py`, `_find_reusable_container`). Apple Container
   has **no `--filter`** and its `--format` takes only `json|table|yaml|toml` —
   no Go templates. A shim that merely translates flags makes that probe fail,
   so Hermes starts a *fresh* container on every launch and never reuses one.

2. **No per-project isolation.** The `/workspace` mount comes from either a
   static `terminal.docker_volumes` entry or the process-wide `TERMINAL_CWD`,
   read once at container-create time. `project_switch` only re-anchors the GUI
   session (`_apply_workspace`) — it never changes what is mounted. So every
   project shares one container and one workspace.

## How this fixes it

The shim implements `ps` faithfully — client-side label filtering plus a
Go-template subset rendered over `container list --format json` — which restores
Hermes' native reuse. On top of that, at `docker run` time it rewrites:

| What | From | To |
|---|---|---|
| `hermes-task-id` label | `default` | `default.alpha` |
| `/workspace` mount source | whatever Hermes passes | the active project's path |
| `/root` sandbox home | `…/sandboxes/docker/default/home` | `…/sandboxes/docker/default.alpha/home` |

Because Hermes keys reuse on that label, **scoping the label is all it takes**
to get one container per project. Hermes then finds the right container by
itself — no name-hijacking, no mapping table, no polling.

The active project comes from Hermes' own database
(`~/.hermes/projects.db`, `project_meta.active_id`) — the same store the desktop
UI and the `project_switch` tool write to.

## Choosing a backend

Pick **one**. The two plugins wrap the same function, so enabling both is not
supported — the installer removes the other when you switch.

| | Apple Container | Docker |
|---|---|---|
| Plugin | `hermes-projects-apple` | `hermes-projects-docker` |
| Needs `docker-wrapper` shim | **yes** (bundled in the plugin) | no |
| Install | `./install-plugins.sh apple` | `./install-plugins.sh docker` |
| Workspace mount driven by | the shim, from the task label | `host_cwd` override in the plugin |
| Extra config needed | none | `docker_mount_cwd_to_workspace: true` |

Apple Container needs the shim because its CLI is not Docker-compatible — no
`--filter`, no Go templates — so Hermes' container-reuse probe fails without
translation. Docker supports all of that natively, so the plugin is enough.

> **Status:** the Apple Container path is used daily and verified end to end.
> The Docker path is unit-tested but has **never been run against a live Docker
> daemon**. Treat it as unverified.

## Install

### Requirements

- Hermes Agent installed at `~/.hermes/hermes-agent` (a git checkout)
- `terminal.backend: docker` in `~/.hermes/config.yaml`
- Apple Container backend: macOS on Apple Silicon with the `container` CLI
  (`brew install container`)
- Docker backend: a running Docker daemon

### Apple Container

```bash
./install-plugins.sh apple
```

That is the whole install. The shim ships **inside** the plugin
(`plugins/hermes-projects-apple/docker-wrapper`), and the plugin points
`HERMES_DOCKER_BINARY` at its own copy at load time — so there is no second
step and no `~/.hermes/.env` edit.

The installer copies the plugin into `~/.hermes/plugins/`, adds it to the
`plugins.enabled` allow-list, and restarts the gateway (required — the plugin
loads into the gateway process).

**Nothing on the system is replaced.** `/usr/local/bin/docker` and anything on
`PATH` are left alone; `HERMES_DOCKER_BINARY` simply outranks them.

<details>
<summary>Optional: <code>./install.sh</code> — develop against the repo</summary>

`install.sh` symlinks `~/.hermes/docker-wrapper` at this repo and writes
`HERMES_DOCKER_BINARY` into `~/.hermes/.env`. Useful when hacking on the shim,
since edits are live with no reinstall — the shim is a fresh process on every
`docker` call. An explicit `HERMES_DOCKER_BINARY` always beats the plugin's
bundled copy, so this cleanly overrides it.

Not needed for a normal install.
</details>

### Docker

```bash
./install-plugins.sh docker
```

Then make the workspace mount follow the project:

```bash
hermes config set terminal.docker_mount_cwd_to_workspace true
```

and remove any fixed `:/workspace` entry from `terminal.docker_volumes` — a
hardcoded host path there would be mounted for *every* project. The plugin logs
a warning at startup if either is wrong.

If you previously used the Apple backend, also clear the shim override, or
Hermes will keep driving Apple Container:

```bash
# in ~/.hermes/.env — remove or comment out
# HERMES_DOCKER_BINARY=/Users/you/.hermes/docker-wrapper
```

### Manual install (no scripts)

The scripts only copy files and edit config — every step is reproducible by
hand. `<repo>` is your clone of this repository.

**1. Copy the plugin.** The Apple plugin bundles the shim, so this one step
carries everything:

```bash
mkdir -p ~/.hermes/plugins
cp -R <repo>/plugins/hermes-projects-apple ~/.hermes/plugins/    # or -docker
```

**2. Copy the shared core.** It is maintained once at `plugins/project_scope.py`
and copied into whichever plugin you installed, so the two can't drift:

```bash
cp <repo>/plugins/project_scope.py ~/.hermes/plugins/hermes-projects-apple/
```

**3. Make the shim executable** (Apple backend only). `find_docker()` skips
anything without the executable bit, and a hand-copied file can lose it:

```bash
chmod +x ~/.hermes/plugins/hermes-projects-apple/docker-wrapper
```

The plugin points `HERMES_DOCKER_BINARY` at this bundled copy itself, so no
`~/.hermes/.env` edit is needed. To use a shim elsewhere, set the variable
explicitly — an explicit value always wins:

```bash
echo 'HERMES_DOCKER_BINARY=/path/to/docker-wrapper' >> ~/.hermes/.env
```

**4. Enable it.** Plugins are opt-in via an allow-list in
`~/.hermes/config.yaml`. Add the block if it isn't there:

```yaml
plugins:
  enabled:
    - hermes-projects-apple      # or hermes-projects-docker
```

**5. Docker backend only** — make the workspace mount follow the project:

```yaml
terminal:
  docker_mount_cwd_to_workspace: true
```

and remove any fixed `:/workspace` entry from `terminal.docker_volumes`.

**6. Remove the file patch if you ever applied it.** It and the plugin must not
both be active — see [The superseded file patch](#the-superseded-file-patch):

```bash
python3 <repo>/patches/per-project-task-id.py --revert
```

**7. Restart the gateway** so the plugin loads:

```bash
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway
```

Confirm with `hermes plugins list` (it should show `enabled`) and:

```bash
grep -a "scoping active" ~/.hermes/logs/agent.log
```

### Switching backends

```bash
./install-plugins.sh docker   # or: apple
```

The installer removes the other plugin, reverts the superseded file patch if
present, and restarts the gateway. Switching **to** Docker additionally needs
the `.env` change above; switching **to** Apple needs nothing extra — the plugin carries its own shim.

Existing containers are not migrated — they keep their old labels and mounts.
Delete them so each project gets a fresh one:

```bash
container list --all                       # or: docker ps -a
container stop <id> && container delete <id>
```

### Verify

```bash
./docker-wrapper version    # Apple backend only -> "build apple-container-shim"

# Which project each container belongs to, and where its workspace points:
container list --all --format json | python3 -c 'import json,sys
for c in json.load(sys.stdin):
    cfg = c["configuration"]
    ws = [m["source"] for m in cfg["mounts"] if m["destination"] == "/workspace"]
    print(cfg["id"], cfg["labels"].get("hermes-task-id"), ws)'

# Every routing decision, with the signal it used:
grep -a "project scope:" ~/.hermes/logs/agent.log | tail
```

A healthy line reads `-> default.<project> via session`. A line reading
`via global active_id` means the session's row was not found and the project was
guessed — see [Known limitation](#known-limitation).

### How Hermes finds the shim

`find_docker()` (`tools/environments/docker.py`) checks `HERMES_DOCKER_BINARY`
**first**, ahead of `PATH`. That matters more than it looks: the gateway's `PATH`
begins with `…/hermes-agent/venv/bin`, and if the
[`docker-for-apple-container`](https://github.com/appautomaton/docker-for-apple-container)
package is pip-installed it puts a `docker` console-script there that shadows
`/usr/local/bin/docker`. `HERMES_DOCKER_BINARY` beats both, so neither needs
uninstalling.

> Two things that mislead people here (both cost time on this project):
> - `terminal.docker_executable` in `config.yaml` is **inert** — nothing in
>   Hermes reads it.
> - Some docs claim `HERMES_DOCKER_BINARY` is unsupported. It is in fact
>   resolution step #1.
>
> Also note `~/.hermes/.env` **overrides** `config.yaml`: e.g.
> `TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE=True` there wins over
> `docker_mount_cwd_to_workspace: false` in the YAML. Check `.env` first when
> the two disagree — and note it is loaded at runtime, so the values never
> appear in `ps eww` output for the gateway process.

### Hermes config

```yaml
terminal:
  backend: docker
  cwd: /workspace
```

You can **delete** any hardcoded `terminal.docker_volumes` workspace entries.
When no `/workspace` volume is given, Hermes mounts its own sandbox workspace
directory there, and the shim repoints that at the active project just the same.

## Container image

Hermes' default image (`nikolaik/python-nodejs`) has Python and Node but no
document tooling, so PDF work in the sandbox fails unless the agent installs
packages first — and those installs are lost whenever a container is recreated.

`image/Dockerfile` bakes them in:

```bash
./image/build.sh          # builds hermes-workspace:latest and verifies it
```

Adds `poppler-utils` (pdftotext, pdftoppm, pdfimages, pdfinfo), `ghostscript`,
`pypdf`, `pdfplumber`, `Pillow`, and for scanned documents `tesseract-ocr` +
`ocrmypdf`.

### OCR

A scanned PDF has no text layer, so `pdftotext` returns nothing. `ocrmypdf`
adds one:

```bash
ocrmypdf --language deu scan.pdf searchable.pdf
pdftotext searchable.pdf -
```

Language data must be present **at build time** — `eng` and `deu` are included.
Add more by extending the `tesseract-ocr-*` list in `image/Dockerfile` (Debian
names follow ISO 639-2: `-fra`, `-spa`, `-nld`) and rebuilding.

`build.sh` verifies OCR end to end rather than trusting version strings: it
generates an image-only PDF, asserts `pdftotext` finds nothing, runs OCR, and
fails the build unless the text comes back. That catches a missing language
pack or a broken ghostscript path, which a `--version` check would not.

Point Hermes at it in **both** places — `~/.hermes/.env` overrides
`config.yaml`, so changing only the YAML has no effect:

```bash
# ~/.hermes/.env
TERMINAL_DOCKER_IMAGE=hermes-workspace:latest
```
```yaml
# ~/.hermes/config.yaml
terminal:
  docker_image: hermes-workspace:latest
```

Then delete the existing containers. Reuse matches on **labels, not image**, so
they would otherwise keep running the old one:

```bash
container list --all
container stop <id> && container delete <id>
```

A stock image works too — `nikolaik/python-nodejs:python3.13-nodejs24` is the
minimum that has Python at all (verified on arm64). An image without Python,
such as `node:24-bookworm-slim`, leaves the agent unable to run any Python.

## Configuration

`~/.hermes/docker-wrapper.json` — all keys optional:

| Key | Default | Meaning |
|---|---|---|
| `per_project` | `true` | Set `false` to make the shim a pure translator (useful for bisecting) |
| `drop_foreign_subworkspace_mounts` | `true` | Drop `…:/workspace/Sub` mounts whose source is outside the active project, instead of leaking one project's files into another |
| `fallback_project_path` | `""` | Workspace to use when no project is active |

Environment:

- `HERMES_PROJECT_SLUG` — force a project, overriding the DB
- `HERMES_WRAPPER_DEBUG=1` — log every translated command to stderr

## Behaviour notes

- **`--network=none` is refused (exit 125)** rather than silently dropped.
  Apple Container has no air-gapped mode; dropping the flag would hand the
  agent network access that Hermes explicitly asked to deny.
- **`--pids-limit`, `--storage-opt`, `--security-opt`, `--privileged`** and
  similar are dropped — Apple Container has no equivalent.
- **`docker exec -i/-t` are preserved.** Apple Container's `exec` does support
  them, despite what some wrapper guides claim.
- **State names are translated**: Apple Container's `stopped` is reported as
  Docker's `exited`, so Hermes' `state != "running"` logic behaves identically.
- **`docker inspect --format`** supports the fields Hermes reads
  (`{{.State.FinishedAt}}`, `{{.HostConfig.NetworkMode}}`). `FinishedAt` is
  always empty — Apple Container does not record it — which Hermes already
  treats as "unknown".

## The plugin (required)

The shim alone is **not sufficient**, and this took a while to establish.
See "Why a Hermes-side change is unavoidable" below for the reasoning.

Install one of the two — they wrap the same function, so never both:

```bash
./install-plugins.sh apple     # Apple Container (uses the shim)
./install-plugins.sh docker    # real Docker (no shim needed)
```

This modifies **no Hermes source files**, so `hermes update` doesn't clobber
it. It installs into `~/.hermes/plugins/` and adds the plugin to the
`plugins.enabled` allow-list in `config.yaml`.

`plugins/project_scope.py` is the single source of truth for both plugins;
`install-plugins.sh` copies it in, so the two can't drift.

### How the project is resolved

Two signals, in order:

1. **The session's own cwd** — `state.db`, `sessions.cwd`, matched against
   project folders by *longest prefix* (so a project nested inside another
   resolves to the inner one).
2. **The globally active project** — `projects.db`, `project_meta.active_id`,
   as a fallback when the session has no usable cwd.

Signal 1 is what makes **concurrent** execution correct. Resolving from the
global `active_id` alone — as the earlier file patch did — means two sessions
running at the same time in different projects both read whichever project was
active at that instant and share a container.

The wrapper calls Hermes' original function first and only scopes a `"default"`
result, so RL/benchmark isolation ids pass through untouched and upstream
changes to that logic are inherited rather than overridden. Any failure falls
back to stock behaviour.

### The superseded file patch

`patches/per-project-task-id.py` does the same job by editing
`tools/terminal_tool.py` directly. The plugin supersedes it — it survives
updates and fixes concurrency. `install-plugins.sh` reverts it automatically
(verified byte-identical to pristine upstream). To revert by hand:

```bash
python3 patches/per-project-task-id.py --revert
```

Do not run both: the patch makes the function return a scoped id, which the
plugin's wrapper then treats as an isolation-keyed task and leaves alone —
silently disabling the per-session resolution.

## Why a Hermes-side change is unavoidable

`_resolve_container_task_id()` collapses every chat session's `task_id` to
`"default"`, and `_active_environments` is keyed on that. So Hermes builds one
environment object per gateway process and reuses it for every chat in every
project — and because the cached object is reused directly, it never shells out
to `docker` again. That decision happens *above* the docker layer: there is no
invocation for a shim to intercept, so no amount of shim work reaches it.

`patches/per-project-task-id.py` scopes that return value by active project, so
the cache keys per project and each project chat builds its own environment.
It is idempotent, backs up the file, syntax-checks the result and restores on
failure. `install.sh` runs it.

**Superseded by the plugin** — kept for reference and for anyone who prefers a
`tools/terminal_tool.py`:

```bash
python3 patches/per-project-task-id.py
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway
```

### Why the idle timer masks this

`terminal.lifetime_seconds` (default 300) drives `_cleanup_inactive_envs()`,
which evicts the cached environment after that much idle time; persist-mode
`cleanup()` leaves the container running. So after ~5 idle minutes the next
message rebuilds the environment, calls the shim, and *does* route correctly.
Switch projects quickly and it doesn't. That intermittency is why this looks
like a flaky mount bug rather than a caching one.

Lowering `lifetime_seconds` is not a fix: the eviction thread polls on a
hardcoded 60s loop, and the orphan reaper deletes labeled containers untouched
for `2 × lifetime_seconds` at startup — so a low value would reap your other
projects' containers on every restart.

## Known limitation

Switching projects *mid-turn* still uses the environment built at the start of
that turn. Any new message picks up the current project.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Covers label scoping and idempotency, project resolution from the DB, the
Go-template subset (including the exact templates Hermes uses), `ps` filter
scoping, volume splitting with spaces in paths, and run-arg rewriting.
