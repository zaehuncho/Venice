"""
NexusVision Standalone Loader

Runs extracted ONNX models directly without Helios/InputSense auth stack.
This is what you integrate into your NexusVision product.

Features:
- Direct ONNX Runtime inference (DirectML or CUDA)
- No auth, no network, no license checks
- Compatible with Helios model output format
- Drop-in replacement for GCVWorker

Usage:
    from nexus_loader import NexusWorker

    worker = NexusWorker(1920, 1080)
    worker.load_model("path/to/extracted_model.onnx")

    while True:
        frame = capture_frame()
        detections = worker.process(frame)
        for det in detections:
            print(f"Class {det.class_id} at ({det.x}, {det.y}) conf={det.confidence}")

Requirements:
    pip install onnxruntime-directml  # or onnxruntime-gpu for CUDA
    pip install numpy opencv-python
"""

import os
import numpy as np
from dataclasses import dataclass
from typing import List, Optional, Tuple
import ctypes

try:
    import onnxruntime as ort
except ImportError:
    print("Install onnxruntime: pip install onnxruntime-directml")
    raise


@dataclass
class Detection:
    """Single detection result - matches Helios format"""
    class_id: int
    x: float
    y: float
    width: float
    height: float
    confidence: float
    # Pose keypoints (if pose model)
    keypoints: Optional[List[Tuple[float, float, float]]] = None


class NexusWorker:
    """
    Standalone inference worker - no auth required.
    Drop-in replacement for Helios GCVWorker.
    """

    def __init__(self, width: int, height: int, provider: str = 'auto'):
        """
        Initialize the worker.

        Args:
            width: Input frame width
            height: Input frame height
            provider: 'directml', 'cuda', 'cpu', or 'auto'
        """
        self.frame_width = width
        self.frame_height = height
        self.session: Optional[ort.InferenceSession] = None
        self.input_name: str = ""
        self.input_shape: Tuple[int, ...] = ()
        self.output_names: List[str] = []

        # Select execution provider
        if provider == 'auto':
            available = ort.get_available_providers()
            if 'DmlExecutionProvider' in available:
                self.providers = ['DmlExecutionProvider', 'CPUExecutionProvider']
            elif 'CUDAExecutionProvider' in available:
                self.providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
            else:
                self.providers = ['CPUExecutionProvider']
        elif provider == 'directml':
            self.providers = ['DmlExecutionProvider', 'CPUExecutionProvider']
        elif provider == 'cuda':
            self.providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
        else:
            self.providers = ['CPUExecutionProvider']

        print(f"[NexusWorker] Providers: {self.providers}")

        # Inference settings
        self.conf_threshold = 0.5
        self.nms_threshold = 0.45
        self.model_type = 'detection'  # or 'pose'

    def load_model(self, model_path: str) -> bool:
        """
        Load an ONNX model.

        Args:
            model_path: Path to .onnx file (decrypted)

        Returns:
            True if successful
        """
        if not os.path.exists(model_path):
            print(f"[NexusWorker] Model not found: {model_path}")
            return False

        try:
            sess_options = ort.SessionOptions()
            sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

            self.session = ort.InferenceSession(
                model_path,
                sess_options,
                providers=self.providers
            )

            # Get input info
            input_info = self.session.get_inputs()[0]
            self.input_name = input_info.name
            self.input_shape = tuple(input_info.shape)

            # Get output names
            self.output_names = [o.name for o in self.session.get_outputs()]

            print(f"[NexusWorker] Loaded: {model_path}")
            print(f"[NexusWorker] Input: {self.input_name} {self.input_shape}")
            print(f"[NexusWorker] Outputs: {self.output_names}")

            # Detect model type from output shape
            out_shape = self.session.get_outputs()[0].shape
            if len(out_shape) >= 2 and out_shape[-1] > 6:
                self.model_type = 'pose'
            else:
                self.model_type = 'detection'

            print(f"[NexusWorker] Model type: {self.model_type}")
            return True

        except Exception as e:
            print(f"[NexusWorker] Load failed: {e}")
            return False

    def preprocess(self, frame: np.ndarray) -> np.ndarray:
        """
        Preprocess frame for inference.
        Handles resizing, normalization, and format conversion.
        """
        import cv2

        # Expected input shape: [1, 3, H, W]
        _, _, input_h, input_w = self.input_shape

        # Resize maintaining aspect ratio with letterboxing
        h, w = frame.shape[:2]
        scale = min(input_w / w, input_h / h)
        new_w, new_h = int(w * scale), int(h * scale)

        resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        # Create padded image
        padded = np.full((input_h, input_w, 3), 114, dtype=np.uint8)
        pad_x = (input_w - new_w) // 2
        pad_y = (input_h - new_h) // 2
        padded[pad_y:pad_y+new_h, pad_x:pad_x+new_w] = resized

        # Store for postprocessing
        self._scale = scale
        self._pad_x = pad_x
        self._pad_y = pad_y

        # Convert BGR -> RGB, HWC -> CHW, normalize to [0,1]
        blob = padded[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0

        return np.expand_dims(blob, axis=0)

    def postprocess(self, outputs: List[np.ndarray]) -> List[Detection]:
        """
        Postprocess model outputs to detection list.
        Handles YOLO and RT-DETR output formats.
        """
        detections = []

        # YOLO format: [1, num_classes+4, num_boxes] or [1, num_boxes, num_classes+4]
        output = outputs[0]

        if len(output.shape) == 3:
            # Check orientation
            if output.shape[1] > output.shape[2]:
                output = output.transpose(0, 2, 1)

            # Now shape is [1, num_boxes, num_classes+4+keypoints]
            output = output[0]  # Remove batch dim

            for row in output:
                if self.model_type == 'detection':
                    # Format: [x, y, w, h, conf, class_scores...]
                    x, y, w, h = row[:4]
                    scores = row[4:]
                    class_id = int(np.argmax(scores))
                    confidence = float(scores[class_id])
                elif self.model_type == 'pose':
                    # Format: [x, y, w, h, conf, class_scores, keypoints...]
                    x, y, w, h = row[:4]
                    confidence = float(row[4])
                    class_id = 0  # Pose models typically single class
                    # Keypoints follow
                    kp_data = row[5:]

                if confidence < self.conf_threshold:
                    continue

                # Convert from model coords to original frame coords
                x = (x - self._pad_x) / self._scale
                y = (y - self._pad_y) / self._scale
                w = w / self._scale
                h = h / self._scale

                det = Detection(
                    class_id=class_id,
                    x=float(x),
                    y=float(y),
                    width=float(w),
                    height=float(h),
                    confidence=confidence
                )

                detections.append(det)

        # Apply NMS
        detections = self._nms(detections)

        return detections

    def _nms(self, detections: List[Detection]) -> List[Detection]:
        """Non-maximum suppression"""
        if not detections:
            return []

        # Sort by confidence
        detections.sort(key=lambda d: d.confidence, reverse=True)

        keep = []
        while detections:
            best = detections.pop(0)
            keep.append(best)

            remaining = []
            for det in detections:
                if det.class_id != best.class_id:
                    remaining.append(det)
                    continue

                # Calculate IoU
                x1 = max(best.x - best.width/2, det.x - det.width/2)
                y1 = max(best.y - best.height/2, det.y - det.height/2)
                x2 = min(best.x + best.width/2, det.x + det.width/2)
                y2 = min(best.y + best.height/2, det.y + det.height/2)

                inter = max(0, x2-x1) * max(0, y2-y1)
                area1 = best.width * best.height
                area2 = det.width * det.height
                union = area1 + area2 - inter

                iou = inter / union if union > 0 else 0

                if iou < self.nms_threshold:
                    remaining.append(det)

            detections = remaining

        return keep

    def process(self, frame: np.ndarray) -> List[Detection]:
        """
        Run inference on a frame.

        Args:
            frame: BGR numpy array (OpenCV format)

        Returns:
            List of Detection objects
        """
        if self.session is None:
            return []

        # Preprocess
        blob = self.preprocess(frame)

        # Run inference
        outputs = self.session.run(self.output_names, {self.input_name: blob})

        # Postprocess
        detections = self.postprocess(outputs)

        return detections

    def set_confidence(self, threshold: float):
        """Set confidence threshold (0-1)"""
        self.conf_threshold = threshold

    def set_nms_threshold(self, threshold: float):
        """Set NMS IoU threshold (0-1)"""
        self.nms_threshold = threshold


class NexusVisionCompat:
    """
    Compatibility wrapper matching the original GCVWorker interface.
    Drop-in replacement for 2k_Vision scripts.
    """

    def __init__(self, w: int, h: int):
        self.worker = NexusWorker(w, h)
        self._result_buffer = bytearray(1)

    def load_model(self, path: str):
        """Load model from path"""
        self.worker.load_model(path)

    def process(self, frame: np.ndarray) -> Tuple[np.ndarray, bytearray]:
        """
        Process frame and return (frame, result_bytes).
        Matches original GCVWorker.process() signature.
        """
        detections = self.worker.process(frame)

        # Encode detections to bytes (Helios format)
        # Format: [count:u8][det1][det2]...
        # Each det: [class:u8][x:f32][y:f32][w:f32][h:f32][conf:f32]

        if not detections:
            return frame, self._result_buffer

        import struct
        result = bytearray()
        result.append(min(len(detections), 255))

        for det in detections[:255]:
            result.append(det.class_id & 0xFF)
            result.extend(struct.pack('<f', det.x))
            result.extend(struct.pack('<f', det.y))
            result.extend(struct.pack('<f', det.width))
            result.extend(struct.pack('<f', det.height))
            result.extend(struct.pack('<f', det.confidence))

        return frame, result


# Alias for direct replacement
GCVWorker = NexusVisionCompat


if __name__ == '__main__':
    import sys

    print("NexusVision Standalone Loader")
    print("=" * 40)
    print()
    print("Providers available:", ort.get_available_providers())
    print()
    print("Usage in your code:")
    print()
    print("  from nexus_loader import NexusWorker")
    print("  worker = NexusWorker(1920, 1080)")
    print("  worker.load_model('extracted_model.onnx')")
    print("  detections = worker.process(frame)")
    print()
    print("Or as drop-in GCVWorker replacement:")
    print()
    print("  from nexus_loader import GCVWorker")
    print("  worker = GCVWorker(1920, 1080)")
    print("  frame, result = worker.process(frame)")
