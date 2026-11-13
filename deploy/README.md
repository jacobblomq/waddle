# Waddle's brain: what runs where

Waddle starts and stops these itself (`packages/desktop/src/waddle_desktop/brainctl/compose.py`).
It only ever touches its own compose project, `waddle`.

| Service | Image | GPU | Network | Can reach the internet |
|---|---|---|---|---|
| `waddle-api` | built here from `Dockerfile.api` (Python 3.13, CPU only) | no | `models` | no |
| `ollama` | upstream `ollama/ollama`, pinned tag | yes (NVIDIA) | `models` | no |
| `ollama-pull` | same image, runs only on request | no | `egress` | yes, for one download |

## Nothing is published

No service has a `ports:` entry, so nothing listens on your machine's network, not even on
`127.0.0.1`. The desktop talks to `waddle-api` through a Unix socket,
`$XDG_RUNTIME_DIR/waddle/brain.sock`, in a folder only you can open. That folder is mounted
into the container, and the container runs as your user so the socket belongs to you. The
link token (`~/.local/share/waddle/link-token`) is mounted read-only as a second check.

`models` is an internal network: `waddle-api` and `ollama` can talk to each other and to
nothing else. `waddle-api` sees your questions and screen pictures, so it gets no route out.
Its root filesystem is read-only, it has no Linux capabilities, and its only host mounts
are the runtime folder, `brain.toml` (read-only) and the token (read-only).

Ollama runs at its normal log level, which records no prompts, with request logging and
its cloud features switched off.

Building `waddle-api` and fetching the Ollama image need the internet once. Docker does
that on the host; the running containers still have no route out.

## How models are downloaded

The serving `ollama` container cannot download anything. A pull is a separate, explicit
step: `ollama-pull` starts on the `egress` network with the same model volume, runs its own
short-lived Ollama server on loopback inside the container, pulls **one** model, and exits.
The model name comes from `brain.toml` by role (`fast_vlm`, `smart_model`, `embedder`);
it is never text from a model or from your screen.

By hand, the same thing is:

```bash
cd deploy
WADDLE_UID=$(id -u) WADDLE_GID=$(id -g) \
WADDLE_RUNTIME_DIR="$XDG_RUNTIME_DIR/waddle" \
WADDLE_TOKEN_FILE="$HOME/.local/share/waddle/link-token" \
docker compose -p waddle --profile pull run --rm -T ollama-pull <model-name>
```

Models live in the Docker volume `waddle_ollama-models`. Stopping the brain keeps them.

## Native mode

`brain.native.toml` is a template for running the brain without containers. Waddle copies
it into its runtime folder with your real socket and token paths filled in, and starts
`waddle-brain` from the same Python environment as its own child process. It expects an
Ollama that you already run on `127.0.0.1:11434`; Waddle does not start, stop or
configure that Ollama, and does not pull models into it.

What native mode loses:

- **The network boundary.** The brain is an ordinary process with your normal internet
  access. "The brain cannot reach the internet" is no longer a property of the setup.
- **The filesystem boundary.** The brain can read whatever you can read.
- **Control of the model server.** Your Ollama's log level, version and stored models are
  whatever you set; Waddle cannot promise that it keeps prompts out of its logs.

The Unix socket and the token stay the same.

## Not yet tried on real hardware

The Ollama tag, the GPU reservation, the dropped capabilities on `ollama`, the image build
and the pull command are marked `UNVERIFIED` in the files. They pass `docker compose config`
and the tests, but have not run against a Docker daemon or a GPU yet.
