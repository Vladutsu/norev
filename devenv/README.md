# Shared dev container

Everyone runs the same image, the same commands and the same dev data (`docs/ARCHITECTURE.md`,
Development environment). You need Docker with Compose v2; on laptop 4 you also need the NVIDIA
Container Toolkit (with podman, see "Laptop 4 with podman").

| Service | Where | What |
|---|---|---|
| `dev` (default) | every laptop | Python 3.12 + `requirements.txt` without the CUDA wheels. No GPU, no models. |
| `gpu` (profile `gpu`) | laptop 4 only | CUDA 12.8 runtime, `requirements.txt` + `requirements-diarization.txt` (NeMo, for the Diarizer), `llama-server` built from llama.cpp v0.9.0 (`cd959ba`). |

Mounts: the repo at `/workspace` (read-write), dev data at `/data` (read-only), models at `/models`
(`gpu` only). Each run first calls `scripts/data.py verify` and stops if `/data` does not match
`data.manifest`.

## Build

```bash
docker compose build dev
```

Laptop 4. `CUDA_ARCHITECTURES` is optional: by default the image is built for 75;86;89;120.
Setting only your GPU's architecture makes the build faster (RTX 30xx = 86, RTX 40xx = 89).

```bash
CUDA_ARCHITECTURES=89 docker compose --profile gpu build gpu
```

Rebuild after `requirements.txt`, `requirements-diarization.txt` or the `Dockerfile` changes. The `gpu`
image is ~12 GB, most of it torch with CUDA for the diarization.

## Run

```bash
docker compose run --rm dev bash check.sh
```

```bash
docker compose run --rm dev bash
```

The LLM runs only on laptop 4. To call its llama-server from another laptop, use synthetic data only:

```bash
LLAMACPP_URL=http://<laptop-4-ip>:8080 docker compose run --rm dev ./norev.sh /data/<clip>.wav ro
```

Laptop 4, the whole pipeline (llama-server is started inside the container):

```bash
docker compose --profile gpu run --rm gpu ./norev.sh /data/<clip>.wav ro
```

Laptop 4, serve llama-server to the other laptops (this opens port 8080 on the local network):

```bash
docker compose --profile gpu run --rm -p 8080:8080 gpu /opt/llama.cpp/llama-server -m /models/qwen3-8b/Qwen3-8B-Q4_K_M.gguf --host 0.0.0.0 --port 8080 -c 16384 --parallel 4 -ngl 99 -fa on -ctk q8_0 -ctv q8_0 --alias Qwen3-8B-Q4_K_M --reasoning off
```

### Laptop 4 with podman

With podman (and docker-compose 1.29), Compose builds the images, but `gpu` starts without the GPU:
podman ignores the GPU reservation in `docker-compose.yml`. Run the same image with the GPU passed
explicitly:

```bash
podman run --rm --device nvidia.com/gpu=all -v .:/workspace -v /workspace/.venv -v ../norev-data:/data:ro -v ../norev-models:/models localhost/norev-gpu ./norev.sh /data/<clip>.wav ro
```

`--device nvidia.com/gpu=all` reads `/etc/cdi/nvidia.yaml`. Regenerate it after every NVIDIA driver update:

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
```

Podman 4.9 cannot read the CDI 0.7.0 spec that current `nvidia-ctk` writes (`unknown field "additionalGids"`):
delete every `additionalGids` field (with its values) from that file.

Environment variables (`README.md`, Configuration) are passed with `-e`, for example
`docker compose run --rm -e NOREV_LANGUAGES=ro,en dev ...`.

## Host folders

| Variable | Default | Mounted at |
|---|---|---|
| `NOREV_HOST_DATA` | `../norev-data` | `/data` (read-only) |
| `NOREV_HOST_MODELS` | `../norev-models` | `/models` (`gpu` only) |

`/models` holds `qwen3-8b/Qwen3-8B-Q4_K_M.gguf` (or another model via `-e LLAMACPP_MODEL=/models/...`). Whisper
and the diarization model (`nvidia/diar_streaming_sortformer_4spk-v2`, 471 MB, CC-BY-4.0, not gated)
download into `/models/huggingface` on first use; to run offline from the start, copy them there from
a host Hugging Face cache (`~/.cache/huggingface/hub/models--...`, with `cp -rL`) and pass
`-e HF_HUB_OFFLINE=1`.

## Dev data

Check it, from the host or the container:

```bash
python3 scripts/data.py verify --data ../norev-data
```

Fetch missing or wrong files into the host folder. `/data` is read-only in the container, so this runs
on the host. `NOREV_DATA_SOURCE` is a local folder or a base URL with the same layout:

```bash
NOREV_DATA_SOURCE=/path/to/shared/norev-data python3 scripts/data.py fetch --data ../norev-data
```

Add a file:

1. Use synthetic Sessions only, never real patient audio.
2. Copy the file into the shared source and into your data folder, e.g. `clips/visit-01.wav`.
3. Append its line to `data.manifest`. The line is `<sha256>  <size in bytes>  <relative path>`:

   ```bash
   cd ../norev-data && echo "$(sha256sum clips/visit-01.wav | cut -d' ' -f1)  $(stat -c %s clips/visit-01.wav)  clips/visit-01.wav"
   ```

4. Run `scripts/data.py verify`, then commit `data.manifest` in a reviewed commit. Never commit the file itself.
