"""
NexusVision Core - Standalone CV inference engine
No dependency on Helios/InputSense auth systems
"""
import os
import sys
import numpy as np

try:
    import onnxruntime as ort
    HAS_ONNX = True
except ImportError:
    HAS_ONNX = False
    print("[!] onnxruntime not installed. Run: pip install onnxruntime-gpu")

class NexusInference:
    """Clean ONNX inference wrapper - no auth, no network calls"""

    def __init__(self, model_path: str, use_gpu: bool = True):
        if not HAS_ONNX:
            raise RuntimeError("onnxruntime required")

        if not os.path.exists(model_path):
            raise FileNotFoundError(f"Model not found: {model_path}")

        # Setup providers
        providers = []
        if use_gpu:
            providers.append('CUDAExecutionProvider')
        providers.append('CPUExecutionProvider')

        # Create session
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self.session = ort.InferenceSession(model_path, opts, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.input_shape = self.session.get_inputs()[0].shape

        # Get output info
        self.outputs = [o.name for o in self.session.get_outputs()]

        print(f"[+] Loaded model: {os.path.basename(model_path)}")
        print(f"    Input: {self.input_name} {self.input_shape}")
        print(f"    Outputs: {self.outputs}")
        print(f"    Provider: {self.session.get_providers()[0]}")

    def preprocess(self, frame: np.ndarray) -> np.ndarray:
        """Preprocess frame for inference"""
        # Resize to model input size if needed
        h, w = frame.shape[:2]
        target_h, target_w = self.input_shape[2], self.input_shape[3]

        if h != target_h or w != target_w:
            import cv2
            frame = cv2.resize(frame, (target_w, target_h))

        # HWC -> CHW
        if len(frame.shape) == 3:
            frame = frame.transpose(2, 0, 1)

        # Add batch dimension
        frame = np.expand_dims(frame, 0)

        # Normalize to float32 [0, 1]
        frame = frame.astype(np.float32) / 255.0

        return frame

    def infer(self, frame: np.ndarray) -> dict:
        """Run inference on preprocessed frame"""
        inputs = {self.input_name: frame}
        outputs = self.session.run(None, inputs)
        return dict(zip(self.outputs, outputs))

    def process(self, frame: np.ndarray) -> dict:
        """Full pipeline: preprocess + infer"""
        preprocessed = self.preprocess(frame)
        return self.infer(preprocessed)


class NexusWorker:
    """Drop-in replacement for Helios GCVWorker"""

    def __init__(self, width: int, height: int, model_path: str = None):
        self.width = width
        self.height = height
        self.model = None
        self._output_buffer = bytearray(1)

        if model_path and os.path.exists(model_path):
            self.model = NexusInference(model_path)

    def process(self, frame: np.ndarray) -> tuple:
        """Process frame, return (frame, output_bytes)"""
        if self.model is None:
            return frame, self._output_buffer

        try:
            results = self.model.process(frame)
            # Encode results to bytes (customize based on model output)
            output = self._encode_results(results)
            return frame, output
        except Exception as e:
            print(f"[!] Inference error: {e}")
            return frame, self._output_buffer

    def _encode_results(self, results: dict) -> bytearray:
        """Encode model outputs to byte format"""
        # This depends on what the model outputs
        # For object detection: encode bounding boxes
        # For classification: encode class scores
        # Default: just return first output as bytes
        if not results:
            return self._output_buffer

        first_output = list(results.values())[0]
        return bytearray(first_output.tobytes())


def create_worker(w: int, h: int, model_path: str = None):
    """Factory function matching Helios interface"""
    return NexusWorker(w, h, model_path)


if __name__ == "__main__":
    print("NexusVision Core")
    print("================")
    print()

    # Test with dummy frame
    import numpy as np

    worker = NexusWorker(1920, 1080)
    dummy_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

    frame, output = worker.process(dummy_frame)
    print(f"[*] Test: frame shape={frame.shape}, output size={len(output)}")
    print("[+] Core initialized successfully")
