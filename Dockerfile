FROM rocm/pytorch:rocm6.1_ubuntu22.04_py3.10_pytorch_2.4

WORKDIR /workspace

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN grep -vE '^(torch|torchvision|torchaudio)([<=>].*)?$' requirements.txt > /tmp/requirements_no_torch.txt \
    && pip install --no-cache-dir -r /tmp/requirements_no_torch.txt


COPY . .

ENV PYTHONPATH=/workspace

CMD ["python", "scripts/train.py", "--config", "configs/experiments.yaml", "--run", "hatebert_multihead"]
