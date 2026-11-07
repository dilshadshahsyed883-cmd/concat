# CUDA/NVENC-only Concat Stage 1 worker for the existing Salad group.
# The native concat-cli / Rust wgpu / Vulkan / Mesa renderer is retired.
# This image DOES NOT modify Salad configuration or deploy to VPS.
FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
# Native Concat project schema pin; Rust/Slint is compiled ONLY for Windows editor.
LABEL org.opencontainers.image.concat-upstream-commit="79116bd0f9a103c633d888603ad27890e929dbdc"
LABEL org.opencontainers.image.concat-project-schema="1"
LABEL org.opencontainers.image.concat-renderer="cuda-nvenc-stage1"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates python3 ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /worker/jobs /worker/outputs /worker/state /worker/.config /worker/.local/share \
    && chown -R 10001:10001 /worker

COPY cloud_render/concat/project_bundle.py /opt/app/cloud_render/concat/project_bundle.py
COPY cloud_render/concat/gpu/worker.py /opt/app/cloud_render/concat/gpu/worker.py
COPY cloud_render/concat/gpu/resumable.py /opt/app/cloud_render/concat/gpu/resumable.py
COPY cloud_render/concat/gpu/simple_timeline_v1.py /opt/app/cloud_render/concat/gpu/simple_timeline_v1.py

# Compile-time software capability gate. The live RTX 3060 Ti and NVIDIA
# decode/encode libraries must still be verified by a real Salad render.
RUN ffmpeg -hide_banner -decoders 2>/dev/null | grep -q mjpeg_cuvid \
    && ffmpeg -hide_banner -decoders 2>/dev/null | grep -q h264_cuvid \
    && ffmpeg -hide_banner -filters 2>/dev/null | grep -q scale_cuda \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_nvenc \
    && command -v ffprobe

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
