# CUDA/NVENC hybrid candidate for Salad.
# The native concat-cli / Rust wgpu / Vulkan / Mesa renderer is retired.
# This image DOES NOT modify Salad configuration or deploy to VPS.
# Compile only the shared, headless native animation generator, not the
# retired Rust video renderer. CUDA still owns all actual frame rendering.
FROM rust:1.93-bookworm AS motion-generator
WORKDIR /src
COPY upstream/src/ ./
RUN cargo build --locked --release -p concat-project --bin concat-motion-expand

FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
ARG SOURCE_SHA=unknown
ENV CONCAT_SOURCE_SHA=$SOURCE_SHA
LABEL org.opencontainers.image.revision=$SOURCE_SHA
# Native Concat project schema pin; Rust/Slint is compiled ONLY for Windows editor.
LABEL org.opencontainers.image.concat-upstream-commit="79116bd0f9a103c633d888603ad27890e929dbdc"
LABEL org.opencontainers.image.concat-project-schema="1,2"
LABEL org.opencontainers.image.concat-renderer="cuda-hybrid-v1"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates python3 python3-venv ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /worker/jobs /worker/outputs /worker/state /worker/.config /worker/.local/share \
    && chown -R 10001:10001 /worker

COPY --from=motion-generator /src/target/release/concat-motion-expand /usr/local/bin/concat-motion-expand
COPY cloud_render/concat/hybrid/ /opt/app/cloud_render/concat/hybrid/
COPY upstream/src/crates/concat-text/fonts/HankenGrotesk-SemiBold.ttf /opt/app/cloud_render/concat/hybrid/HankenGrotesk-SemiBold.ttf
COPY upstream/src/crates/concat-text/fonts/ /opt/app/cloud_render/concat/hybrid/fonts/
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: \
       -r /opt/app/cloud_render/concat/hybrid/requirements-codec.txt \
       -r /opt/app/cloud_render/concat/hybrid/requirements-captions.txt \
       -r /opt/app/cloud_render/concat/hybrid/requirements-asr.txt \
       nvidia-cuda-runtime-cu12==12.8.90 nvidia-cuda-nvrtc-cu12==12.8.93
# Preseed the desktop's default small ASR model; other explicitly selected
# sizes may still download into this writable cache when requested.
RUN /opt/venv/bin/python -c "from faster_whisper import download_model; download_model('small', cache_dir='/worker/asr-models')" \
    && chown -R 10001:10001 /worker/asr-models
ENV CONCAT_ASR_MODEL_CACHE=/worker/asr-models
ENV PATH=/opt/venv/bin:$PATH
ENV LD_LIBRARY_PATH=/opt/venv/lib/python3.12/site-packages/nvidia/cublas/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cudnn/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cuda_nvrtc/lib:/usr/local/nvidia/lib64
COPY cloud_render/concat/project_bundle.py /opt/app/cloud_render/concat/project_bundle.py
# Reuse the verified complete-worker packaging fix, including acceptance CLI.
COPY cloud_render/concat/gpu/ /opt/app/cloud_render/concat/gpu/
COPY upstream/src/crates/concat/text-presets/ /opt/app/cloud_render/concat/gpu/text-presets/

# Compile-time software capability gate. The live RTX 3060 Ti and NVIDIA
# decode/encode libraries must still be verified by a real Salad render.
RUN ffmpeg -hide_banner -decoders 2>/dev/null | grep -q mjpeg_cuvid \
    && ffmpeg -hide_banner -decoders 2>/dev/null | grep -q h264_cuvid \
    && ffmpeg -hide_banner -filters 2>/dev/null | grep -q scale_cuda \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_nvenc \
    && command -v ffprobe \
    && printf '%s' '{"version":2,"timelines":[]}' | concat-motion-expand > /dev/null

ENV PYTHONPATH=/opt/app
ENV PYTHONUNBUFFERED=1
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics
ENV PORT=8080
ENV HOME=/worker
ENV XDG_CONFIG_HOME=/worker/.config
ENV XDG_DATA_HOME=/worker/.local/share

WORKDIR /worker
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python3", "-m", "cloud_render.concat.gpu.worker"]
