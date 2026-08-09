# NexusVision

Standalone CV inference engine — no auth dependencies.

## Components

### nexus_core.py
Clean ONNX inference wrapper. Drop-in replacement for Helios GCVWorker.

```python
from nexus_core import NexusWorker

# Initialize with model
worker = NexusWorker(1920, 1080, "models/your_model.onnx")

# Process frames
frame, output = worker.process(cv2_frame)
```

### model_extractor.py
Runtime hook to capture decrypted models from memory.

```bash
# 1. Start Helios
# 2. Run extractor
python model_extractor.py
# 3. Start a script in Helios that loads a model
# 4. Models saved to ./models/
```

## Setup

```bash
pip install onnxruntime-gpu numpy opencv-python frida frida-tools
```

## Usage Flow

1. Run `model_extractor.py` while Helios loads a protected script
2. Decrypted models are saved to `./models/`
3. Use `NexusWorker` with extracted models — no auth required
