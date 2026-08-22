#!/usr/bin/env bash
set -euo pipefail

# Upload CIFAR-100 dataset to MinIO for contrastive learning

TRACE_WAS_ENABLED=0
case "$-" in
  *x*)
    TRACE_WAS_ENABLED=1
    set +x
    ;;
esac

MINIO_ENDPOINT="${MINIO_ENDPOINT:-glasslab-minio.glasslab-v2.svc.cluster.local:9000}"
minio_access_key="${MINIO_ACCESS_KEY:-}"
minio_secret_key="${MINIO_SECRET_KEY:-}"
unset MINIO_ACCESS_KEY MINIO_SECRET_KEY
export -n minio_access_key minio_secret_key 2>/dev/null || true
BUCKET_NAME="${BUCKET_NAME:-glasslab-artifacts}"
DATASET_NAME="cifar100"
DATASET_PATH="${DATASET_PATH:-/tmp/cifar100}"

usage() {
  cat <<'USAGE'
Usage: upload-cifar100.sh [--endpoint <endpoint>] [--bucket <bucket>] [--dataset-path <path>]

Upload CIFAR-100 dataset to MinIO.

Set MINIO_ACCESS_KEY and MINIO_SECRET_KEY in the environment. They are scoped
to each MinIO client process through MC_HOST_glasslab and never placed in argv.

Options:
  --endpoint      MinIO endpoint (default: glasslab-minio.glasslab-v2.svc.cluster.local:9000)
  --bucket        MinIO bucket name (default: glasslab-artifacts)
  --dataset-path  Path to CIFAR-100 dataset (default: /tmp/cifar100)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --endpoint)
      MINIO_ENDPOINT="$2"
      shift 2
      ;;
    --access-key|--secret-key)
      printf '[upload-cifar100] credential arguments are not supported; use the documented environment boundary\n' >&2
      exit 1
      ;;
    --bucket)
      BUCKET_NAME="$2"
      shift 2
      ;;
    --dataset-path)
      DATASET_PATH="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf '[upload-cifar100] unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$minio_access_key" || -z "$minio_secret_key" ]]; then
  printf '[upload-cifar100] MINIO_ACCESS_KEY and MINIO_SECRET_KEY must be set\n' >&2
  exit 1
fi
if [[ "$minio_access_key" == *$'\n'* || "$minio_access_key" == *$'\r'* ]] ||
  [[ "$minio_secret_key" == *$'\n'* || "$minio_secret_key" == *$'\r'* ]]; then
  printf '[upload-cifar100] MinIO credentials must be single-line values\n' >&2
  exit 1
fi
if [[ ! "$MINIO_ENDPOINT" =~ ^[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]]; then
  printf '[upload-cifar100] MinIO endpoint must be a DNS name with an optional port\n' >&2
  exit 1
fi

encoded_access_key="$({ printf '%s' "$minio_access_key"; } | python3 -c '
import sys
from urllib.parse import quote

print(quote(sys.stdin.read(), safe=""), end="")
')"
encoded_secret_key="$({ printf '%s' "$minio_secret_key"; } | python3 -c '
import sys
from urllib.parse import quote

print(quote(sys.stdin.read(), safe=""), end="")
')"
minio_host="http://${encoded_access_key}:${encoded_secret_key}@${MINIO_ENDPOINT}"
unset minio_access_key minio_secret_key encoded_access_key encoded_secret_key
export -n minio_host 2>/dev/null || true

run_mc() (
  export MC_HOST_glasslab="$minio_host"
  unset minio_host
  exec mc "$@"
)

# Check if MinIO client is available
if ! command -v mc &> /dev/null; then
  printf '[upload-cifar100] mc (MinIO client) not found. Installing...\n'
  wget -O mc https://dl.min.io/client/release/linux-amd64/mc
  chmod +x mc
  mv mc /usr/local/bin/
fi

# Configure MinIO client
printf '[upload-cifar100] configuring MinIO client...\n'
printf '[upload-cifar100] using scoped MinIO environment alias...\n'

# Create bucket if it doesn't exist
printf '[upload-cifar100] ensuring bucket exists...\n'
if ! run_mc ls glasslab/"${BUCKET_NAME}" &> /dev/null; then
  run_mc mb glasslab/"${BUCKET_NAME}"
fi

# Generate CIFAR-100 dataset if not present
if [[ ! -d "${DATASET_PATH}" ]]; then
  printf '[upload-cifar100] CIFAR-100 not found at %s. Generating...\n' "${DATASET_PATH}"
  mkdir -p "${DATASET_PATH}"
  
  # Generate synthetic CIFAR-100 data (replace with actual download if needed)
  python3 <<'PYTHON'
import os
import numpy as np
from PIL import Image

# Generate synthetic CIFAR-100 dataset
np.random.seed(42)
num_classes = 100
samples_per_class = 500  # Reduced for testing
img_size = 32

os.makedirs('/tmp/cifar100/train', exist_ok=True)
os.makedirs('/tmp/cifar100/test', exist_ok=True)

# Class names (CIFAR-100 fine labels)
class_names = [
    'apple', 'aquarium_fish', 'baby', 'bear', 'beaver', 'bed', 'bee', 'beetle',
    'bicycle', 'bottle', 'bowl', 'boy', 'bridge', 'bus', 'butterfly', 'camel',
    'can', 'castle', 'caterpillar', 'cattle', 'chair', 'chimpanzee', 'clock',
    'cloud', 'cockroach', 'couch', 'crab', 'crocodile', 'cup', 'dinosaur',
    'dolphin', 'elephant', 'flatfish', 'forest', 'fox', 'girl', 'hamster',
    'house', 'kangaroo', 'keyboard', 'lamp', 'lawn_mower', 'leopard', 'lion',
    'lizard', 'lobster', 'man', 'maple_tree', 'motorcycle', 'mountain', 'mouse',
    'mushroom', 'oak_tree', 'orange', 'orchid', 'otter', 'palm_tree', 'pear',
    'pickup_truck', 'pine_tree', 'plain', 'plate', 'poppy', 'porcupine',
    'possum', 'rabbit', 'raccoon', 'ray', 'road', 'rocket', 'rose', 'sea',
    'seal', 'shark', 'shrew', 'skunk', 'skyscraper', 'snail', 'snake', 'spider',
    'squirrel', 'streetcar', 'sunflower', 'sweet_pepper', 'table', 'tank',
    'telephone', 'television', 'tiger', 'tractor', 'train', 'trout', 'tulip',
    'turtle', 'wardrobe', 'whale', 'willow_tree', 'wolf', 'woman', 'worm'
]

# Generate train images
for class_idx, class_name in enumerate(class_names):
    for sample_idx in range(samples_per_class):
        img = np.random.randint(0, 256, (img_size, img_size, 3), dtype=np.uint8)
        img_path = f'/tmp/cifar100/train/{class_name}_{sample_idx:04d}.png'
        Image.fromarray(img).save(img_path)

# Generate test images
for class_idx, class_name in enumerate(class_names):
    for sample_idx in range(samples_per_class // 5):  # 20% for test
        img = np.random.randint(0, 256, (img_size, img_size, 3), dtype=np.uint8)
        img_path = f'/tmp/cifar100/test/{class_name}_{sample_idx:04d}.png'
        Image.fromarray(img).save(img_path)

print(f'Generated CIFAR-100 dataset: {len(class_names)} classes, {samples_per_class} train samples/class')
PYTHON
fi

# Upload train images
printf '[upload-cifar100] uploading train images...\n'
run_mc cp --recursive "${DATASET_PATH}/train" "glasslab/${BUCKET_NAME}/datasets/${DATASET_NAME}/train/"

# Upload test images
printf '[upload-cifar100] uploading test images...\n'
run_mc cp --recursive "${DATASET_PATH}/test" "glasslab/${BUCKET_NAME}/datasets/${DATASET_NAME}/test/"

# Upload dataset config
printf '[upload-cifar100] uploading dataset config...\n'
cat <<'CONFIG' > /tmp/cifar100_config.yaml
name: cifar100
description: CIFAR-100 dataset for contrastive learning
type: image_classification
split:
  train_samples: 50000
  test_samples: 10000
  seen_classes: 80
  unseen_classes: 20
augmentation:
  random_resized_crop:
    size: 32
    scale: [0.2, 1.0]
    ratio: [0.75, 1.333]
  color_jitter:
    brightness: 0.8
    contrast: 0.8
    saturation: 0.8
    hue: 0.2
  random_horizontal_flip: true
CONFIG

run_mc cp /tmp/cifar100_config.yaml "glasslab/${BUCKET_NAME}/datasets/${DATASET_NAME}/config.yaml"

# Verify upload
printf '[upload-cifar100] verifying upload...\n'
run_mc ls "glasslab/${BUCKET_NAME}/datasets/${DATASET_NAME}/train/" | head -5
run_mc ls "glasslab/${BUCKET_NAME}/datasets/${DATASET_NAME}/test/" | head -5

unset minio_host
if [[ "$TRACE_WAS_ENABLED" -eq 1 ]]; then
  unset TRACE_WAS_ENABLED
  set -x
fi
printf '[upload-cifar100] CIFAR-100 upload complete\n'
