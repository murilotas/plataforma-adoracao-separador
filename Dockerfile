FROM pytorch/pytorch:2.2.2-cuda12.1-cudnn8-runtime
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 TORCH_HOME=/opt/models SEPARATION_DEVICE=cuda
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg espeak-ng && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy==1.26.4 demucs==4.0.1 runpod==1.7.13
# Preload the two open models at build time, before any billed GPU execution.
RUN python -c "from demucs.pretrained import get_model; get_model('htdemucs'); get_model('htdemucs_6s'); get_model('htdemucs_ft')"
WORKDIR /app
COPY separate-tracks.py runpod-handler.py /app/
RUN useradd --create-home --uid 10001 processor && chmod -R a+rX /opt/models /app
USER processor
WORKDIR /home/processor
CMD ["python", "-u", "/app/runpod-handler.py"]
