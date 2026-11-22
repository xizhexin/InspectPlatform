"""可插拔检测器。业务层只依赖 Detector 协议，换模型不改一行业务代码。

已实测路径：StubDetector（合成帧）。
可选路径：OnnxDetector（需 onnxruntime + numpy），未接真实权重，仅提供接口与解码钩子。
"""
from __future__ import annotations

import random
from typing import Protocol


class Detector(Protocol):
    version: str

    def infer(self, frame) -> list[dict]:
        """返回 [{'cls': str, 'score': float, 'box': tuple}, ...]"""
        ...


class StubDetector:
    """合成帧驱动的检测器，用于全链路测试、压测与演示。

    明确声明：它不是训练好的模型，不产生任何"精度"数字；
    检索/去重/降级/工单链路的指标与它无关，是业务系统自身的指标。
    """

    def __init__(self, version="stub-1.0", fail_rate=0.0, seed=None):
        self.version = version
        self.fail_rate = fail_rate
        self._rng = random.Random(seed)

    def infer(self, frame):
        if self.fail_rate and self._rng.random() < self.fail_rate:
            raise RuntimeError("detector backend unavailable (simulated)")
        return [
            {"cls": d["cls"], "score": float(d["score"]),
             "box": tuple(d.get("box", ()))}
            for d in frame.get("defects", [])
        ]


class OnnxDetector:
    """ONNX Runtime 推理适配。decode 由调用方按模型输出头注入。

    本机 monai 环境(3.11)已装 onnxruntime 1.22 / numpy；隔离环境(3.13)未装，
    import 失败时由 InspectionService 自动降级，业务主流程不受影响。
    """

    def __init__(self, model_path, labels, decode=None, version="onnx-1.0",
                 providers=None):
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError(
                "onnxruntime/numpy not installed; fall back to StubDetector"
            ) from exc
        self.np = np
        self.labels = labels
        self.version = version
        self.decode = decode or self._simple_decode
        self.sess = ort.InferenceSession(
            model_path, providers=providers or ["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name

    def infer(self, frame):
        raw = self.sess.run(None, {self.input_name: frame["tensor"]})[0]
        return self.decode(raw, self.labels, self.np)

    @staticmethod
    def _simple_decode(raw, labels, np, conf=0.25, top_k=20):
        """[1, N, 5+C] 输出的极简解码：取每 anchor 最大类别分。

        注意：这里不做 NMS / letterbox 还原；接真实 YOLO 时请注入 decode=...。
        """
        arr = np.asarray(raw)
        if arr.ndim == 3:
            arr = arr[0]
        out = []
        for row in arr:
            scores = row[5:] if row.shape[0] > 5 else row[4:5]
            idx = int(np.argmax(scores))
            score = float(scores[idx])
            if score < conf:
                continue
            name = labels[idx] if idx < len(labels) else f"class_{idx}"
            out.append({"cls": name, "score": score, "box": tuple(row[:4].tolist())})
        return sorted(out, key=lambda d: -d["score"])[:top_k]
